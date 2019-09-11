# -*- encoding: utf-8 -*-
"""
Audit

This module provides access to an easy format for writing audit checks in
Hubble.

Audit checks target a specific audit module, such as ``grep`` or ``fdg``. They
are formed via YAML files in your hubblestack_data source:

.. code-block:: yaml

    CIS-6.2.4:
      grep.grep:
        args:
          - /etc/group
        kwargs:
          pattern: '^+:'
          fail_on_match: True
        description: Ensure no legacy "+" entries exist in /etc/group

All audit checks have a few things in common. They have an ``id``, a ``tag``, a
``description``, and a ``module.function`` (usually with arguments). The ``id`` will be used as the
tag if a tag is not explicitly provided.

.. code-block:: yaml

    <id>
      <module.function>:
        args:
          - arg1
          - arg2
        kwargs:
          foo: bar
        description: <description>
        version: <version>
        tag: <tag>  # Uses <id> if not defined
        target: <target>
        labels:
          - <label>

You may have noticed there are a few more features shown in the example above.
Here's how they work:

target:
    Allows you to use a Salt-style compound match to target this check to
    specific hosts. If a host doesn't match the target, it won't execute the
    check.

labels:
    Allows labels to be applied to a check. Labels will be reported in the
    results, and can also be targeted by audit runs so only checks with a given
    label will be executed.

version:
    Allows checks to be limited to certain versions of hubble. Version
    requirements can be of the following forms:

    <3.0.0
    <=3.0.0
    >3.0.0
    >=3.0.0

    Multiple version requirements can be used, separated by commas and
    semicolons. Commas will be processed first, and will result in AND logic.
    Semicolons will then be processed, using OR logic to combine any existing
    results. So, to have a check only run on version 3.0 and 3.2 and later
    (but not run on 3.1) you might do something like this:

    version: '>=3.0.0,<3.1.0;>=3.2.0'

    Note that all checks use distutils.StrictVersion, so this will not work with
    non-standard hubble releases.

    Checks skipped by version checks will be returned in a separate 'Skipped'
    key, so that you can track when hosts are skipping targeted checks because
    they haven't updated Hubble

Like many pieces of Hubble, you can utilize topfiles to target files with one
or more audit check to specific sets of hosts. Targeting is done via Salt-style
compound matching:

.. code-block:: yaml

    audit:
      '*':
        - cis.linux
        - adobebaseline
      'G@os:CoreOS':
        - coreos_additional_checks

Audit modules take arbitrary args and kwargs and return a tuple
``(success, data_dict)`` where ``success`` is a boolean True/False on whether
it was a success or a failure, and ``data_dict`` is a dictionary of any
information that should be added to the check's data dictionary in the return.
"""
from __future__ import absolute_import

import fnmatch
import logging
import os
import salt.loader
import salt.utils
import yaml

from distutils.version import StrictVersion
from hubblestack.status import HubbleStatus
from salt.exceptions import CommandExecutionError

LOG = logging.getLogger(__name__)

hubble_status = HubbleStatus(__name__, 'top', 'audit')

__audit__ = None


@hubble_status.watch
def audit(audit_files=None,
          tags='*',
          labels=None,
          verbose=True,
          show_success=True):
    """
    Execute one or more audit files, and return the cumulative results.

    :param audit_files:
        Which audit files to execute. Can contain multiple files (list or
        comma-separated).
    :param tags:
        Can be used to target a subset of tags via glob targeting.
    :param labels:
        Only run the checks with the given label(s). Can contain multiple
        labels (comma-separated). If multiple labels are provided, a check
        with any label in the list will be run.
    :param verbose:
        True by default. If set to False, results will be trimmed to just tags
        and descriptions.
    :param show_success:
        Whether to show successes/skipped or just failures. Defaults to True
    :return:
        Returns dictionary with Success, Skipped, and Failure keys and the
        results of the checks
    """
    ret = {'Success': [],
           'Failure': [],
           'Skipped': []}

    if not audit_files:
        LOG.warning('audit.audit called without any audit_files')
        return ret

    if not isinstance(audit_files, list):
        audit_files = audit_files.split(',')

    audit_files = ['salt://hubblestack_audit/' + audit_file.replace('.', '/') + '.yaml'
                   for audit_file in audit_files]

    if labels is None:
        labels = []
    if not isinstance(labels, list):
        labels = labels.split(',')

    # Load audit modules
    global __audit__
    __audit__ = salt.loader.LazyLoader(salt.loader._module_dirs(__opts__, 'audit'),
                                       __opts__,
                                       tag='audit',
                                       pack={'__salt__': __salt__,
                                             '__grains__': __grains__})

    for audit_file in audit_files:
        # Cache audit file
        path = __salt__['cp.cache_file'](audit_file)

        # Fileserver will return False if the file is not found
        if not path:
            LOG.error('Could not find audit file {0}'.format(audit_file))
            continue

        # Load current audit file
        audit_data = None
        if os.path.isfile(path):
            try:
                with open(path, 'r') as fh:
                    audit_data = yaml.safe_load(fh)
            except Exception as e:
                LOG.exception('Error loading audit file {0}: {1}'.format(audit_file, e))
                continue
        if not audit_data or not isinstance(audit_data, dict):
            LOG.error('audit data from {0} was not formed as a dict'.format(audit_file))
            continue

        ret = _run_audit(ret, audit_data, tags, labels, audit_file)

    # If verbose=False, reduce each check to a dictionary with {tag: description}
    if not verbose or verbose == 'False':
        succinct_ret = {'Success': [],
                        'Failure': [],
                        'Skipped': []}
        for success_type, checks in ret.iteritems():
            for check in checks:
                succinct_ret[success_type].append({check['tag']: check.get('description', '<no description>')})

        ret = succinct_ret

    # Remove successes/skipped if show_success is False
    if not show_success or show_success == 'False':
        ret.pop('Success')
        ret.pop('Skipped')
    elif not ret['Skipped']:
        ret.pop('Skipped')


    return ret


@hubble_status.watch
def top(topfile='salt://hubblestack_audit/top.audit',
        tags='*',
        labels=None,
        verbose=True,
        show_success=True):
    """
    Given a topfile with a series of compound targets, compile a list of audit
    files for this host and execute and return the results of those audit files.

    .. code-block:: yaml

        audit:
          '*':
            - cis.linux
            - adobebaseline
          'G@os:CoreOS':
            - coreos_additional_checks

    :param topfile:
        Topfile to process for targeted audit files to execute.
    :param tags:
        See audit()
    :param labels:
        See audit()
    :param verbose:
        See audit()
    :param show_success:
        See audit()
    :return:
        Returns dictionary with Success and Failure keys and the results of the
        checks
    """
    audit_files = _get_top_data(topfile)

    return audit(audit_files,
                 tags=tags,
                 labels=labels,
                 verbose=verbose,
                 show_success=show_success,
                 )


def _get_top_data(topfile):

    topfile = __salt__['cp.cache_file'](topfile)

    if not topfile:
        raise CommandExecutionError('Topfile not found.')

    try:
        with open(topfile) as handle:
            topdata = yaml.safe_load(handle)
    except Exception as e:
        raise CommandExecutionError('Could not load topfile: {0}'.format(e))

    if not isinstance(topdata, dict) or 'audit' not in topdata or \
            not isinstance(topdata['audit'], dict):
        raise CommandExecutionError('Audit topfile not formatted correctly.')

    topdata = topdata['audit']

    ret = []

    for match, data in topdata.iteritems():
        if __salt__['match.compound'](match):
            ret.extend(data)

    return ret


def _version_cmp(version):
    """
    Handle version comparison for audit checks

    :param version:
        Version comparison string. See module-level documentation for more
        details.
    :return:
        Boolean as to whether the versions match
    """
    # '>=3.0.0,<3.1.0;>=3.2.0'
    versions = version.split(';')
    # ['>=3.0.0,<3.1.0', '>=3.2.0']
    versions = [item.split(',') for item in versions]
    # [['>=3.0.0', '<3.1.0'], ['>=3.2.0']]
    processed_versions = []
    for item in versions:
        # Inner matches (comma separator) are AND
        overall_match = True
        for comparison in item:
            if not comparison:
                match = False
            elif comparison.startswith('<='):
                comparison = comparison[2:]
                match = StrictVersion(__grains__['hubble_version']) <= StrictVersion(comparison)
            elif comparison.startswith('<'):
                comparison = comparison[1:]
                match = StrictVersion(__grains__['hubble_version']) < StrictVersion(comparison)
            elif comparison.startswith('>='):
                comparison = comparison[2:]
                match = StrictVersion(__grains__['hubble_version']) >= StrictVersion(comparison)
            elif comparison.startswith('>'):
                comparison = comparison[1:]
                match = StrictVersion(__grains__['hubble_version']) > StrictVersion(comparison)
            else:  # Equals, by default
                match = StrictVersion(__grains__['hubble_version']) == StrictVersion(comparison)
            # If we ever get a False, this whole AND block will be marked as False
            overall_match = overall_match and match
        processed_versions.append(overall_match)

    # Outer matches (semicolon separator) are OR
    version_match = False
    for item in processed_versions:
        if item:
            version_match = True

    return version_match


def _run_audit(ret, audit_data, tags, labels, audit_file):
    """

    :param ret:
        The dictionary of return data from audit()
    :param audit_data:
        The audit checks to be run
    :param tags:
        See audit()
    :param labels:
        See audit()
    :return:
        Returns the updated ``ret`` object
    """
    for audit_id, data in audit_data.iteritems():
        LOG.debug('Executing audit id {0} in audit file {1}'.format(audit_id, audit_file))
        try:
            module = data.keys()[0]
            data = data[module]
            if not isinstance(data, dict):
                LOG.error('Audit data with id {0} from file {1} not formatted '
                          'correctly'.format(audit_id, audit_file))
                continue
        except (IndexError, NameError) as e:
            LOG.exception('Audit data with id {0} from file {1} not formatted '
                          'correctly'.format(audit_id, audit_file))
            continue

        tag = data.get('tag', audit_id)
        data['tag'] = tag
        data['id'] = audit_id
        data['file'] = audit_file
        label_list = data.get('labels', [])
        version = data.get('version')

        # Process tags via globbing
        if not fnmatch.fnmatch(tag, tags):
            LOG.debug('Skipping audit {0} due to tag {1} not matching tags {2}'
                      .format(audit_id, tag, tags))
            continue

        # Process labels
        matching_label = False
        if not labels:
            matching_label = True
        for label in labels:
            if label in label_list:
                matching_label = True
        if not matching_label:
            LOG.debug('Skipping audit {0} due to no matching labels {1} in label list {2}'
                      .format(audit_id, labels, label_list))
            continue

        # Process target
        target = data.get('target', '*')
        if not __salt__['match.compound'](target):
            LOG.debug('Skipping audit {0} due to target mismatch: {1}'.format(target))
            continue

        # Process any version targeting
        if version and 'hubble_version' not in __grains__:
            LOG.error('Audit {0} calls for version checking {1} but cannot '
                      'find `hubble_version` in __grains__. Skipping.'
                      .format(audit_id, version))
            ret['Skipped'].append({audit_id: data})
            continue
        elif version:
            version_match = _version_cmp(version)
            if not version_match:
                LOG.debug('Skipping audit {0} due to version {1} not matching '
                          'version requirements {2}'
                          .format(audit_id, __grains__['hubble_version'], version))
                ret['Skipped'].append({audit_id: data})
                continue

        args = data.get('args', [])
        kwargs = data.get('kwargs', {})

        # Run the audit
        try:
            success, data_dict = __audit__[module](*args, **kwargs)
        except Exception as e:
            LOG.error('Audit {0} from file {1} failed with exception {2}'
                      .format(audit_id, audit_file, e))
            data['reason'] = 'exception'
            data['exception'] = str(e)
            ret['Failure'].append({audit_id: data})
            continue

        if data_dict and isinstance(data_dict, dict):
            data_dict.update(data)
        else:
            data_dict = data

        if success:
            ret['Success'].append({audit_id: data_dict})
        else:
            ret['Failure'].append({audit_id: data_dict})

    return ret
