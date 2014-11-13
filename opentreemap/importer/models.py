# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import json
from datetime import datetime

from django.db.models import Count
from django.contrib.gis.db import models

from treemap.models import User, Instance

from importer import errors


class GenericImportEvent(models.Model):

    class Meta:
        abstract = True

    PENDING_VERIFICATION = 1
    VERIFIYING = 2
    FINISHED_VERIFICATION = 3
    CREATING = 4
    FINISHED_CREATING = 5
    FAILED_FILE_VERIFICATION = 6

    # Original Name of the file
    file_name = models.CharField(max_length=255)

    # Global errors and notices (json)
    errors = models.TextField(default='')

    field_order = models.TextField(default='')

    # Metadata about this particular import
    owner = models.ForeignKey(User)
    instance = models.ForeignKey(Instance)
    created = models.DateTimeField(auto_now=True)
    completed = models.DateTimeField(null=True, blank=True)

    status = models.IntegerField(default=PENDING_VERIFICATION)

    # When false, this dataset is in 'preview' mode
    # When true this dataset has been written to the
    # database
    commited = models.BooleanField(default=False)

    def status_summary(self):
        if self.status == GenericImportEvent.PENDING_VERIFICATION:
            return "Not Yet Started"
        elif self.status == GenericImportEvent.VERIFIYING:
            return "Verifying"
        elif self.status == GenericImportEvent.FINISHED_VERIFICATION:
            return "Verification Complete"
        elif self.status == GenericImportEvent.CREATING:
            return "Creating Trees"
        elif self.status == GenericImportEvent.FAILED_FILE_VERIFICATION:
            return "Invalid File Structure"
        else:
            return "Finished"

    def active(self):
        return self.status != GenericImportEvent.FINISHED_CREATING

    def is_running(self):
        return (
            self.status == self.VERIFIYING or
            self.status == self.CREATING)

    def is_finished(self):
        return (
            self.status == self.FINISHED_VERIFICATION or
            self.status == self.FINISHED_CREATING or
            self.status == self.FAILED_FILE_VERIFICATION)

    def row_counts_by_status(self):
        q = self.row_set()\
                .values('status')\
                .annotate(Count('status'))

        return {r['status']: r['status__count'] for r in q}

    def completed_row_count(self):
        n_left = self.row_counts_by_status().get(GenericImportRow.WAITING, 0)
        return self.row_set().count() - n_left

    def update_status(self):
        """ Update the status field based on current row statuses """
        pass

    def append_error(self, err, data=None):
        code, msg, fatal = err

        if self.errors is None or self.errors == '':
            self.errors = '[]'

        self.errors = json.dumps(
            self.errors_as_array() + [
                {'code': code,
                 'msg': msg,
                 'data': data,
                 'fatal': fatal}])

        return self

    def errors_as_array(self):
        if self.errors is None or self.errors == '':
            return []
        else:
            return json.loads(self.errors)

    def has_errors(self):
        return len(self.errors_as_array()) > 0

    def row_set(self):
        raise Exception('Abstract Method')

    def rows(self):
        return self.row_set().order_by('idx').all()

    def validate_main_file(self):
        raise Exception('Abstract Method')

    def _validate_main_file(self, datasource, fieldsource,
                            validate_custom_fields):
        """
        Make sure the imported file has rows and valid columns
        """
        is_valid = True

        # This is a fatal error. We need to have at least
        # one row to get header info
        if datasource.count() == 0:
            is_valid = False
            self.append_error(errors.EMPTY_FILE)
        else:
            datastr = datasource[0].data
            input_fields = set(json.loads(datastr).keys())

            custom_error = validate_custom_fields(input_fields)
            if custom_error is not None:
                is_valid = False
                self.append_error(custom_error)

            # It is a warning if there are extra input fields
            rem = input_fields - fieldsource
            if len(rem) > 0:
                is_valid = False
                self.append_error(errors.UNMATCHED_FIELDS, list(rem))

        if not is_valid:
            self.status = self.FAILED_FILE_VERIFICATION
            self.save()

        return is_valid


class GenericImportRow(models.Model):
    """
    A row of data and import status
    Subclassed by 'Tree Import Row' and 'Species Import Row'
    """

    class Meta:
        abstract = True

    # JSON dictionary from header <-> rows
    data = models.TextField()

    # Row index from original file
    idx = models.IntegerField()

    finished = models.BooleanField(default=False)

    # JSON field containing error information
    errors = models.TextField(default='')

    # Status
    SUCCESS = 0
    ERROR = 1
    WARNING = 2
    WAITING = 3
    VERIFIED = 4

    status = models.IntegerField(default=WAITING)

    def __init__(self, *args, **kwargs):
        super(GenericImportRow, self).__init__(*args, **kwargs)
        self.jsondata = None
        self.cleaned = {}

    @property
    def model_fields(self):
        raise Exception('Abstract Method')

    @property
    def datadict(self):
        if self.jsondata is None:
            self.jsondata = json.loads(self.data)

        return self.jsondata

    @datadict.setter
    def datadict(self, v):
        self.jsondata = v
        self.data = json.dumps(self.jsondata)

    def errors_as_array(self):
        if self.errors is None or self.errors == '':
            return []
        else:
            return json.loads(self.errors)

    def has_errors(self):
        return len(self.errors_as_array()) > 0

    def get_fields_with_error(self):
        data = {}
        datadict = self.datadict

        for e in self.errors_as_array():
            for field in e['fields']:
                data[field] = datadict[field]

        return data

    def has_fatal_error(self):
        if self.errors:
            for err in json.loads(self.errors):
                if err['fatal']:
                    return True

        return False

    def append_error(self, err, fields, data=None):
        code, msg, fatal = err

        if self.errors is None or self.errors == '':
            self.errors = '[]'

        # If you give append_error a single field
        # there is no need to get angry
        if isinstance(fields, basestring):
            fields = (fields,)  # make into tuple

        self.errors = json.dumps(
            json.loads(self.errors) + [
                {'code': code,
                 'fields': fields,
                 'msg': msg,
                 'data': data,
                 'fatal': fatal}])

        return self

    def safe_float(self, fld):
        try:
            return float(self.datadict[fld])
        except:
            self.append_error(errors.FLOAT_ERROR, fld)
            return False

    def safe_bool(self, fld):
        """ Returns a tuple of (success, bool value) """
        v = self.datadict.get(fld, '').lower()

        if v == '':
            return (True, None)
        if v == 'true' or v == 't' or v == 'yes':
            return (True, True)
        elif v == 'false' or v == 'f' or v == 'no':
            return (True, False)
        else:
            self.append_error(errors.BOOL_ERROR, fld)
            return (False, None)

    def safe_int(self, fld):
        try:
            return int(self.datadict[fld])
        except:
            self.append_error(errors.INT_ERROR, fld)
            return False

    def safe_pos_int(self, fld):
        i = self.safe_int(fld)

        if i is False:
            return False
        elif i < 0:
            self.append_error(errors.POS_INT_ERROR, fld)
            return False
        else:
            return i

    def safe_pos_float(self, fld):
        i = self.safe_float(fld)

        if i is False:
            return False
        elif i < 0:
            self.append_error(errors.POS_FLOAT_ERROR, fld)
            return False
        else:
            return i

    def convert_units(self, data, converts):
        # TODO: Convert using instance's per-field units choice
        INCHES_TO_DBH_FACTOR = 1.0  # / settings.DBH_TO_INCHES_FACTOR

        # Similar to tree
        for fld, factor in converts.iteritems():
            if fld in data and factor != 1.0:
                data[fld] = float(data[fld]) * factor * INCHES_TO_DBH_FACTOR

    def validate_numeric_fields(self):
        def cleanup(fields, fn):
            has_errors = False
            for f in fields:
                if f in self.datadict and self.datadict[f]:
                    maybe_num = fn(f)

                    if maybe_num is False:
                        has_errors = True
                    else:
                        self.cleaned[f] = maybe_num

            return has_errors

        pfloat_ok = cleanup(self.model_fields.POS_FLOAT_FIELDS,
                            self.safe_pos_float)

        float_ok = cleanup(self.model_fields.FLOAT_FIELDS,
                           self.safe_float)

        int_ok = cleanup(self.model_fields.POS_INT_FIELDS,
                         self.safe_pos_int)

        return pfloat_ok and float_ok and int_ok

    def validate_boolean_fields(self):
        has_errors = False
        for f in self.model_fields.BOOLEAN_FIELDS:
            if f in self.datadict:
                success, v = self.safe_bool(f)
                if success and v is not None:
                    self.cleaned[f] = v
                else:
                    has_errors = True

        return has_errors

    def validate_string_fields(self):
        has_errors = False
        for field in self.model_fields.STRING_FIELDS:

            value = self.datadict.get(field, None)
            if value:
                if len(value) > 255:
                    self.append_error(errors.STRING_TOO_LONG, field)
                    has_errors = True
                else:
                    self.cleaned[field] = value

        return has_errors

    def validate_date_fields(self):
        has_errors = False
        for field in self.model_fields.DATE_FIELDS:
            value = self.datadict.get(field, None)
            if value:
                try:
                    datep = datetime.strptime(value, '%Y-%m-%d')
                    self.cleaned[self.model_fields.DATE_PLANTED] = datep
                except ValueError:
                    self.append_error(errors.INVALID_DATE,
                                      self.model_fields.DATE_PLANTED)
                    has_errors = True

        return has_errors

    def validate_and_convert_datatypes(self):
        self.validate_numeric_fields()
        self.validate_boolean_fields()
        self.validate_string_fields()
        self.validate_date_fields()

    def validate_row(self):
        """
        Validate a row. Returns True if there were no fatal errors,
        False otherwise

        The method mutates self in two ways:
        - The 'errors' field on self will be appended to
          whenever an error is found
        - The 'cleaned' field on self will be set as fields
          get validated
        """
        raise Exception('Abstract Method')