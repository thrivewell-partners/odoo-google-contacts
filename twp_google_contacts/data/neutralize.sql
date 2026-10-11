-- A copied database must never reach the Google accounts its original connected.
DELETE FROM ir_config_parameter WHERE key = 'twp_google_contacts.broker_key';
