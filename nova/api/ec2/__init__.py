# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Starting point for routing EC2 requests.

"""

import logging
import webob
import webob.dec
import webob.exc

from nova import context
from nova import exception
from nova import flags
from nova import utils
from nova import wsgi
from nova.api.ec2 import apirequest
from nova.auth import manager


FLAGS = flags.FLAGS
flags.DEFINE_boolean('use_forwarded_for', False,
                     'Treat X-Forwarded-For as the canonical remote address. '
                     'Only enable this if you have a sanitizing proxy.')
flags.DEFINE_integer('lockout_attempts', 5,
                     'Number of failed auths before lockout.')
flags.DEFINE_integer('lockout_minutes', 15,
                     'Number of minutes to lockout if triggered.')
flags.DEFINE_integer('lockout_window', 15,
                     'Number of minutes for lockout window.')
flags.DEFINE_list('lockout_memcached_servers', None,
                  'Memcached servers or None for in process cache.')


_log = logging.getLogger("api")
_log.setLevel(logging.DEBUG)


class Lockout(wsgi.Middleware):
    """Lockout for x minutes on y failed auths in a z minute period.

    x = lockout_timeout flag
    y = lockout_window flag
    z = lockout_attempts flag

    Uses memcached if lockout_memcached_servers flag is set, otherwise it
    uses a very simple in-proccess cache. Due to the simplicity of
    the implementation, the timeout window is started with the first
    failed request, so it will block if there are x failed logins within
    that period.

    There is a possible race condition where simultaneous requests could
    sneak in before the lockout hits, but this is extremely rare and would
    only result in a couple of extra failed attempts."""

    def __init__(self, application):
        """middleware can use fake for testing."""
        if FLAGS.lockout_memcached_servers:
            import memcache
        else:
            from nova import fakememcache as memcache
        self.mc = memcache.Client(FLAGS.lockout_memcached_servers,
                                  debug=0)
        super(Lockout, self).__init__(application)

    @webob.dec.wsgify
    def __call__(self, req):
        access_key = str(req.params['AWSAccessKeyId'])
        failures_key = "authfailures-%s" % access_key
        failures = int(self.mc.get(failures_key) or 0)
        if failures >= FLAGS.lockout_attempts:
            detail = "Too many failed authentications."
            raise webob.exc.HTTPForbidden(detail=detail)
        res = req.get_response(self.application)
        if res.status_int == 403:
            failures = self.mc.incr(failures_key)
            if failures is None:
                # NOTE(vish): To use incr, failures has to be a string.
                self.mc.set(failures_key, '1', time=FLAGS.lockout_window * 60)
            elif failures >= FLAGS.lockout_attempts:
                _log.warn('Access key %s has had %d failed authentications'
                          ' and will be locked out for %d minutes.' %
                          (access_key, failures, FLAGS.lockout_minutes))
                self.mc.set(failures_key, str(failures),
                            time=FLAGS.lockout_minutes * 60)
        return res


class Authenticate(wsgi.Middleware):

    """Authenticate an EC2 request and add 'ec2.context' to WSGI environ."""

    @webob.dec.wsgify
    def __call__(self, req):
        # Read request signature and access id.
        try:
            signature = req.params['Signature']
            access = req.params['AWSAccessKeyId']
        except:
            raise webob.exc.HTTPBadRequest()

        # Make a copy of args for authentication and signature verification.
        auth_params = dict(req.params)
        # Not part of authentication args
        auth_params.pop('Signature')

        # Authenticate the request.
        try:
            (user, project) = manager.AuthManager().authenticate(
                    access,
                    signature,
                    auth_params,
                    req.method,
                    req.host,
                    req.path)
        except exception.Error, ex:
            logging.debug(_("Authentication Failure: %s") % ex)
            raise webob.exc.HTTPForbidden()

        # Authenticated!
        remote_address = req.remote_addr
        if FLAGS.use_forwarded_for:
            remote_address = req.headers.get('X-Forwarded-For', remote_address)
        ctxt = context.RequestContext(user=user,
                                      project=project,
                                      remote_address=remote_address)
        req.environ['ec2.context'] = ctxt
        return self.application


class Requestify(wsgi.Middleware):

    def __init__(self, app, controller_name):
        super(Requestify, self).__init__(app)
        self.controller = utils.import_class(controller_name)()

    @webob.dec.wsgify
    def __call__(self, req):
        non_args = ['Action', 'Signature', 'AWSAccessKeyId', 'SignatureMethod',
                    'SignatureVersion', 'Version', 'Timestamp']
        args = dict(req.params)
        try:
            # Raise KeyError if omitted
            action = req.params['Action']
            for non_arg in non_args:
                # Remove, but raise KeyError if omitted
                args.pop(non_arg)
        except:
            raise webob.exc.HTTPBadRequest()
        api_request = apirequest.APIRequest(self.controller, action, args)
        req.environ['ec2.request'] = api_request
        req.environ['ec2.action_args'] = args
        return self.application


class Authorizer(wsgi.Middleware):

    """Authorize an EC2 API request.

    Return a 401 if ec2.controller and ec2.action in WSGI environ may not be
    executed in ec2.context.
    """

    def __init__(self, application):
        super(Authorizer, self).__init__(application)
        self.action_roles = {
            'CloudController': {
                'DescribeAvailabilityzones': ['all'],
                'DescribeRegions': ['all'],
                'DescribeSnapshots': ['all'],
                'DescribeKeyPairs': ['all'],
                'CreateKeyPair': ['all'],
                'DeleteKeyPair': ['all'],
                'DescribeSecurityGroups': ['all'],
                'AuthorizeSecurityGroupIngress': ['netadmin'],
                'RevokeSecurityGroupIngress': ['netadmin'],
                'CreateSecurityGroup': ['netadmin'],
                'DeleteSecurityGroup': ['netadmin'],
                'GetConsoleOutput': ['projectmanager', 'sysadmin'],
                'DescribeVolumes': ['projectmanager', 'sysadmin'],
                'CreateVolume': ['projectmanager', 'sysadmin'],
                'AttachVolume': ['projectmanager', 'sysadmin'],
                'DetachVolume': ['projectmanager', 'sysadmin'],
                'DescribeInstances': ['all'],
                'DescribeAddresses': ['all'],
                'AllocateAddress': ['netadmin'],
                'ReleaseAddress': ['netadmin'],
                'AssociateAddress': ['netadmin'],
                'DisassociateAddress': ['netadmin'],
                'RunInstances': ['projectmanager', 'sysadmin'],
                'TerminateInstances': ['projectmanager', 'sysadmin'],
                'RebootInstances': ['projectmanager', 'sysadmin'],
                'UpdateInstance': ['projectmanager', 'sysadmin'],
                'DeleteVolume': ['projectmanager', 'sysadmin'],
                'DescribeImages': ['all'],
                'DeregisterImage': ['projectmanager', 'sysadmin'],
                'RegisterImage': ['projectmanager', 'sysadmin'],
                'DescribeImageAttribute': ['all'],
                'ModifyImageAttribute': ['projectmanager', 'sysadmin'],
                'UpdateImage': ['projectmanager', 'sysadmin'],
            },
            'AdminController': {
                # All actions have the same permission: ['none'] (the default)
                # superusers will be allowed to run them
                # all others will get HTTPUnauthorized.
            },
        }

    @webob.dec.wsgify
    def __call__(self, req):
        context = req.environ['ec2.context']
        controller = req.environ['ec2.request'].controller.__class__.__name__
        action = req.environ['ec2.request'].action
        allowed_roles = self.action_roles[controller].get(action, ['none'])
        if self._matches_any_role(context, allowed_roles):
            return self.application
        else:
            raise webob.exc.HTTPUnauthorized()

    def _matches_any_role(self, context, roles):
        """Return True if any role in roles is allowed in context."""
        if context.user.is_superuser():
            return True
        if 'all' in roles:
            return True
        if 'none' in roles:
            return False
        return any(context.project.has_role(context.user.id, role)
                   for role in roles)


class Executor(wsgi.Application):

    """Execute an EC2 API request.

    Executes 'ec2.action' upon 'ec2.controller', passing 'ec2.context' and
    'ec2.action_args' (all variables in WSGI environ.)  Returns an XML
    response, or a 400 upon failure.
    """

    @webob.dec.wsgify
    def __call__(self, req):
        context = req.environ['ec2.context']
        api_request = req.environ['ec2.request']
        result = None
        try:
            result = api_request.invoke(context)
        except exception.ApiError as ex:
            if ex.code:
                return self._error(req, ex.code, ex.message)
            else:
                return self._error(req, type(ex).__name__, ex.message)
        # TODO(vish): do something more useful with unknown exceptions
        except Exception as ex:
            return self._error(req, type(ex).__name__, str(ex))
        else:
            resp = webob.Response()
            resp.status = 200
            resp.headers['Content-Type'] = 'text/xml'
            resp.body = str(result)
            return resp

    def _error(self, req, code, message):
        logging.error("%s: %s", code, message)
        resp = webob.Response()
        resp.status = 400
        resp.headers['Content-Type'] = 'text/xml'
        resp.body = str('<?xml version="1.0"?>\n'
                     '<Response><Errors><Error><Code>%s</Code>'
                     '<Message>%s</Message></Error></Errors>'
                     '<RequestID>?</RequestID></Response>' % (code, message))
        return resp


class Versions(wsgi.Application):

    @webob.dec.wsgify
    def __call__(self, req):
        """Respond to a request for all EC2 versions."""
        # available api versions
        versions = [
            '1.0',
            '2007-01-19',
            '2007-03-01',
            '2007-08-29',
            '2007-10-10',
            '2007-12-15',
            '2008-02-01',
            '2008-09-01',
            '2009-04-04',
        ]
        return ''.join('%s\n' % v for v in versions)


def authenticate_factory(global_args, **local_args):
    def authenticator(app):
        return Authenticate(app)
    return authenticator


def authorizer_factory(global_args, **local_args):
    def authorizer(app):
        return Authorizer(app)
    return authorizer


def executor_factory(global_args, **local_args):
    return Executor()


def versions_factory(global_args, **local_args):
    return Versions()

def requestify_factory(global_args, **local_args):
    def requestifier(app):
        return Requestify(app, local_args['controller'])
    return requestifier

def lockout_factory(global_args, **local_args):
    def locksmith(app):
        return Lockout(app)
    return locksmith
