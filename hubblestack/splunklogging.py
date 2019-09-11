"""
Hubblestack python log handler for splunk

Uses the same configuration as the rest of the splunk returners, returns to
the same destination but with an alternate sourcetype (``hubble_log`` by
default)

.. code-block:: yaml

    hubblestack:
      returner:
        splunk:
          - token: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
            indexer: splunk-indexer.domain.tld
            index: hubble
            sourcetype_log: hubble_log

You can also add an `custom_fields` argument which is a list of keys to add to events
with using the results of config.get(<custom_field>). These new keys will be prefixed
with 'custom_' to prevent conflicts. The values of these keys should be
strings or lists (will be sent as CSV string), do not choose grains or
pillar values with complex values or they will be skipped:

.. code-block:: yaml

    hubblestack:
      returner:
        splunk:
          - token: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
            indexer: splunk-indexer.domain.tld
            index: hubble
            sourcetype_log: hubble_log
            custom_fields:
              - site
              - product_group
"""
import socket

# Imports for http event forwarder
import copy
import time
import logging
from hubblestack.hec import http_event_collector, get_splunk_options, make_hec_args
import hubblestack.utils.stdrec



class SplunkHandler(logging.Handler):
    """
    Log handler for splunk
    """
    def __init__(self):
        super(SplunkHandler, self).__init__()

        self.opts_list = get_splunk_options()
        self.endpoint_list = []

        for opts in self.opts_list:
            custom_fields = opts['custom_fields']

            # Set up the fields to be extracted at index time. The field values must be strings.
            # Note that these fields will also still be available in the event data
            index_extracted_fields = []
            try:
                index_extracted_fields.extend(__opts__.get('splunk_index_extracted_fields', []))
            except TypeError:
                pass

            # Set up the collector
            args, kwargs = make_hec_args(opts)
            hec = http_event_collector(*args, **kwargs)

            fqdn = SplunkHandler._get_fqdn(__grains__['id'])

            event = {}
            event.update(hubblestack.utils.stdrec.std_info())

            for custom_field in custom_fields:
                custom_field_name = 'custom_' + custom_field
                custom_field_value = __salt__['config.get'](custom_field, '')
                if isinstance(custom_field_value, (str, unicode)):
                    event.update({custom_field_name: custom_field_value})
                elif isinstance(custom_field_value, list):
                    custom_field_value = ','.join(custom_field_value)
                    event.update({custom_field_name: custom_field_value})

            payload = {}
            payload.update({'host': fqdn})
            payload.update({'index': opts['index']})
            payload.update({'sourcetype': opts['sourcetype']})

            # Potentially add metadata fields:
            fields = {}
            for item in index_extracted_fields:
                if item in event and not isinstance(event[item], (list, dict, tuple)):
                    fields["meta_%s" % item] = str(event[item])
            if fields:
                payload.update({'fields': fields})

            self.endpoint_list.append((hec, event, payload))

    @staticmethod
    def _get_fqdn(minion_id):
        '''
        Extract fqdn from __grains__, check if it is valid and, if not,
        get it using another method.
        '''
        fqdn = __grains__['fqdn']
        # Sometimes fqdn is blank. If it is, replace it with minion_id
        fqdn = fqdn if fqdn else minion_id
        try:
            fqdn_ip4 = __grains__.get('local_ip4')
            if not fqdn_ip4:
                fqdn_ip4 = __grains__['fqdn_ip4'][0]
        except IndexError:
            try:
                fqdn_ip4 = __grains__['ipv4'][0]
            except IndexError:
                raise Exception('No ipv4 grains found. Is net-tools installed?')
        if fqdn_ip4.startswith('127.'):
            for ip4_addr in __grains__['ipv4']:
                if ip4_addr and not ip4_addr.startswith('127.'):
                    fqdn_ip4 = ip4_addr
                    break

        # Sometimes fqdn reports a value of localhost. If that happens, try another method.
        bad_fqdns = ['localhost', 'localhost.localdomain', 'localhost6.localdomain6']
        if fqdn in bad_fqdns:
            new_fqdn = socket.gethostname()
            if '.' not in new_fqdn or new_fqdn in bad_fqdns:
                new_fqdn = fqdn_ip4
            fqdn = new_fqdn

        return fqdn

    def emit(self, record):
        """
        Emit a single record using the hec/event template/payload template
        generated in __init__()
        """

        # NOTE: poor man's filtering ... goal: prevent logging loops and
        # various objects from logging to splunk in an infinite spiral of spam.
        # This might be more stylish as a logging.Filter, but that would need
        # to be re-added everywhere SplunkHandler is added to the logging tree.
        # Also, we don't wish to filter the logging, only to filter it from
        # splunk; so any logging.Filter would need to be very carefully added
        # to work right.

        # NOTE: we used to use 'pathname', rather than 'name' here.  That can't
        # be made to work when hubble is packaged in a binary (every single
        # 'pathname' comes through as logging/__init__.py for some reason).
        #
        # Matching 'name' works, but relies on devs using getLogger(__name__)
        # and not some other arbitrary string.

        filtered = ('hubblestack.splunklogging', 'hubblestack.hec', 'urllib3.connectionpool')
        rpn = getattr(record, 'name', '')
        for i in filtered:
            if i in rpn:
                return False

        log_entry = SplunkHandler.format_record(record)
        for hec, event, payload in self.endpoint_list:
            event = copy.deepcopy(event)
            payload = copy.deepcopy(payload)
            event.update(log_entry)
            payload['event'] = event
            # no_queue tells the hec never to queue the data to disk
            hec.batchEvent(payload, eventtime=time.time(), no_queue=True)
            hec.flushBatch()
        return True

    @staticmethod
    def format_record(record):
        """
        Format the log record into a dictionary for easy insertion into a
        splunk event dictionary
        """
        try:
            log_entry = {'message': record.message,
                         'level': record.levelname,
                         'timestamp': int(time.time()),
                         'loggername': record.name,
                        }
        except Exception:
            log_entry = {'message': record.msg,
                         'level': record.levelname,
                         'loggername': record.name,
                         'timestamp': int(time.time()),
                        }
        return log_entry
