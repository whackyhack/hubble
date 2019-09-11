# -*- encoding: utf-8 -*-
"""
HubbleStack Nova module for auditing open ports.

Sample data for the netstat whitelist:

.. code-block:: yaml

    netstat:
        ssh:
            address: '*:22'
        another_identifier:
            address:
              - 127.0.0.1:80
              - 0.0.0.0:80
"""
from __future__ import absolute_import

import copy
import fnmatch
import logging

import salt.utils

log = logging.getLogger(__name__)


def __virtual__():
    if 'network.netstat' in __salt__:
        return True
    return False, 'No network.netstat function found'


def audit(data_list, tags, labels, debug=True, **kwargs):
    """
    Run the network.netstat command
    """
    ret = {'Success': [], 'Failure': []}

    __tags__ = {}
    for profile, data in data_list:
        if 'netstat' in data:
            for check, check_args in data['netstat'].iteritems():
                if 'address' in check_args:
                    tag_args = copy.deepcopy(check_args)
                    tag_args['id'] = check
                    tag_args['nova_profile'] = profile
                    if isinstance(check_args['address'], list):
                        for address in check_args['address']:
                            __tags__[address] = tag_args
                    else:
                        __tags__[check_args['address']] = tag_args

    if not __tags__:
        # No yaml data found, don't do any work
        return ret

    for address_data in __salt__['network.netstat']():

        success = False
        for whitelisted_address in __tags__:
            if fnmatch.fnmatch(address_data['local-address'], whitelisted_address):
                address_data.update({
                    'tag': __tags__[whitelisted_address]['address'][0],
                    'description': __tags__[whitelisted_address]['id'],
                    'nova_profile': __tags__[whitelisted_address]['nova_profile']
                })
                ret['Success'].append(address_data)
                success = True
                break
        if success is False:
            address_data.update({
                'tag': address_data['local-address'],
                'description': address_data['program'],
                'nova_profile': 'netstat'
            })
            ret['Failure'].append(address_data)

    return ret
