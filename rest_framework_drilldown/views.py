from django.conf import settings
from django.db import connection
from django.db.models.fields.related import ForeignKey, OneToOneField, ManyToManyField, RelatedObject
from django.core.exceptions import FieldError
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView


class DrillDownAPIView(APIView):
    """
    Subclass this to create an instant GET API with fields, filters, etc.

    Supports the following GET request parameters (shown with examples):
        format:
            format=json

        limit and offset:
            limit=10&offset=60

        order_by:
            order_by=-client.profile.first_name  <  order by associated client's first name, in reverse order

        fields:
            fields=id,client.profile.first_name < returns the item id and associated client's first name
            fields=client  < returns the associated client (will just be an id)
            fields=client.ALL   < ALL keyword, will return the entire record (flat) for the associated client
            (Note that ability to drill down to a sub-model is constrained by drilldowns setting in the view.)

        other parameters are treated as filters:
            client.profile.first_name__istartswith=pete  < associated client's name starts with 'pete'
            paid=true  < paid is true (True/true and False/false will all work)
            amount__gt=100  < amount greater than 100
            (Multiple filters can be combined. Filterable objects are also constrained to those in the drilldowns list.)

    Returns results with header codes:
        X-Total-Count: the total match count before applying limit or offset
        X-Query_Error: any errors, usually returned with status 400
    """
    GLOBAL_THROTTLE = 2000  # global max result count

    drilldowns = None  # override this to allow drilldowns into sub-objects
    ignore = None  # override this with fieldnames that should be ignored in the GET request
    hide = None
    model = None     # override this with the model

    def __init__(self, *args, **kwargs):
        self.error = ''

        # deal with None's that should be arrays
        self.ignore = self.ignore or []
        self.drilldowns = self.drilldowns or []
        self.hide = self.hide or []

        # These will go into the query
        self.select_relateds = []
        self.prefetch_relateds = []

        # Non-model fields that may be in the query
        self.hide_fields = [h.replace('__', '.') for h in self.hide]
        self.ignore_fields = set(['fields', 'limit', 'offset', 'format', 'order_by'] + self.ignore + self.hide_fields)

        super(DrillDownAPIView, self).__init__(*args, **kwargs)

    def get_base_query(self):   # override this to return your base query
        return None

    def get(self, request):
        """The main method in this object; handles a GET request with filters, fields, etc."""
        if settings.DEBUG:
            num_queries = len(connection.queries)  # just for testing

        data = {}
        headers = {}

        def _result(status=200):
            return Response(data,
                            headers=headers,
                            status=status)

        def _error(msg='Error'):
            print msg
            headers['X-Query_Error'] = msg
            return _result(status=400)

        fields = self.request.QUERY_PARAMS.get('fields', [])
        fields = fields and fields.split(',')  # fields parameter is a comma-delimited list

        # get parameters that will be used as filters
        filters = {}
        request_params = self.request.QUERY_PARAMS
        for f in request_params:
            if f.split('__')[0] not in self.ignore_fields:   # split so you catch things like "invoice.total__lt=10000"
                filters[f] = request_params[f]

        qs = self.get_base_query()
        if qs is None:
            return _error('API error: get_base_query() missing or invalid')

        # Get the complete set of drilldowns
        self.drilldowns = self._validate_drilldowns(self.drilldowns)
        if self.error:
            return _error(self.error)

        # Create the fields_map (a multi-level dictionary describing the fields to be returned)
        self.fields_map = self._create_fields_map(fields)
        if self.error:
            return _error(self.error)

        # Get filters, validate against drilldowns
        self.filter_kwargs = self._set_filter_kwargs(filters)
        if self.error:
            return _error(self.error)

        # Add our relateds to the query
        if self.select_relateds:
            qs = qs.select_related(*self.select_relateds)
        if self.prefetch_relateds:
            qs = qs.prefetch_related(*self.prefetch_relateds)

        # Add our filters to the query
        try:
            qs = qs.filter(**self.filter_kwargs)
        except FieldError:
            qs = qs.none()
            return _error('Bad filter parameter in query.')
        except ValueError:
            qs = qs.none()
            return _error('Bad filter value in query')
        queryset_for_count = qs  # saving this off so we can use it later before we add limits

        # Deal with ordering
        order_by = self.request.QUERY_PARAMS.get('order_by', '').replace('.', '__')
        # would be nice to validate order_by fields here, but difficult, so we just trap below
        if order_by:
            order_by = order_by.split(',')
            qs = qs.order_by(*order_by)

        # Deal with offset and limit
        self.offset = int_or_none(self.request.QUERY_PARAMS.get('offset')) or 0
        self.limit = int_or_none(self.request.QUERY_PARAMS.get('limit')) or 0
        self.limit = min(getattr(self, 'THROTTLE', self.GLOBAL_THROTTLE), self.limit or self.GLOBAL_THROTTLE)
        if self.limit and self.offset:
            qs = qs[self.offset:self.limit + self.offset]
        elif self.limit:
            qs = qs[:self.limit]
        elif self.offset:
            qs = qs[self.offset:]

        # create the chained serializer
        serializer = DrilldownSerializerFactory(self.model)(fields_map=self.fields_map, instance=qs, many=True)

        # return the response
        try:
            data = serializer.data
        except FieldError:
            return _error('Error: May be bad field name in order_by')  # typical error

        # get total count if 1) your count = the limit, or 2) the query has an offset.
        if self.offset or (len(data) and len(data) == self.limit):
            total_count = queryset_for_count.count()
        else:
            total_count = len(data)
        headers = {'X-Total-Count': total_count}
        if settings.DEBUG:
            headers['X-Query-Count'] = len(connection.queries) - num_queries
        return _result()

    #  Various Methods  #
    # Validate the list of drilldowns and fill in any gaps; returns array of drilldowns
    def _validate_drilldowns(self, drilldowns):
        ERROR_STRING = 'Error in drilldowns'
        validated_drilldowns = []

        def validate_me(current_model, dd_string, current_string=''):
            pair = dd_string.split('__', 1)
            fieldname = (pair[0]).strip()
            if not is_field_in(current_model, fieldname):
                self.error = ('%s: "%s" is not a valid field in %s. Remember __ not .)' %
                              (ERROR_STRING, fieldname, current_model.__name__))
                return None
            new_model = get_model(current_model, fieldname)
            if not new_model:
                self.error = ('%s: "%s" is not a ForeignKey, ManyToMany, OneToOne, or RelatedObject.'
                              % (ERROR_STRING, fieldname))
                return None
            current_string = (current_string + '__' + fieldname).strip('__')

            # note that we add missing intermediate models, e.g. 'client' if list included 'client__profile'
            if current_string not in validated_drilldowns:
                validated_drilldowns.append(current_string)
                # if there's more, keep drilling
            if len(pair) > 1:
                validate_me(new_model, pair[1], current_string)  # recursion

        for dd in drilldowns:
            validate_me(self.model, dd)
        if ERROR_STRING in self.error:
            validated_drilldowns = []
        return validated_drilldowns

    def _create_fields_map(self, fields):
        """Take the list of fields submitted in the query and turn it into a multi-level tree dict"""
        fields_map = {}
        ERROR_STRING = 'Error in fields'

        def add_to_fields_map(current_model, current_map, dot_string, current_related=''):
            pair = dot_string.split('.', 1)
            fieldname = (pair[0]).strip()
            there_are_subfields = len(pair) > 1
            if not (fieldname == 'ALL' or is_field_in(current_model, fieldname)):  # ALL is allowed in fields_map
                self.error = ('%s: "%s" is not a valid field' % (ERROR_STRING, dot_string))
                return None

            if fieldname == 'ALL':
                # add in all the fields for the model
                fname_prefix = current_related.replace('__', '.') + '.'
                for fname in current_model._meta.get_all_field_names():
                    if (fname_prefix + fname).strip('.') in self.hide_fields:
                        continue  # skip it
                    field_type = get_field_type(current_model, fname)
                    # don't add the field if it's a related field and out of drilldowns range
                    if field_type in [ManyToManyField]:
                        temp = (current_related + '__' + fname).strip('__')
                        if temp not in self.drilldowns:
                            continue  # don't add this one
                    add_to_fields_map(current_model, current_map, dot_string=fname, current_related=current_related)
            else:
                # add it to the map
                if current_map.get(fieldname) is None:
                    current_map[fieldname] = {}
                    # drill down one level in the map
                current_map = current_map[fieldname]
                # see if the field is a related one
                new_model = get_model(current_model, fieldname)
                field_type = get_field_type(current_model, fieldname)
                if new_model and (field_type == ManyToManyField or there_are_subfields):
                    # Add field to select_related or prefetch_relateds
                    current_related = (current_related + '__' + fieldname).strip('__')
                    if current_related in self.drilldowns:
                        field_type = get_field_type(current_model, fieldname)
                        if field_type in [ForeignKey, OneToOneField, RelatedObject]:
                            self.select_relateds.append(current_related)
                        else:
                            self.prefetch_relateds.append(current_related)
                    else:
                        self.error = ('%s: %s is not valid' % (ERROR_STRING, current_related.replace('__', '.')))
                        return None

                    # Add sub-field to fields_map
                    if there_are_subfields:
                        add_to_fields_map(new_model, current_map, pair[1], current_related)  # recurse
                    else:
                        add_to_fields_map(new_model, current_map, 'id')  # defaults to return the id only
                elif there_are_subfields:  # requested a sub-field for a field that's not a model, e.g. amount.profile
                    self.error = ('%s: %s not valid field' % (ERROR_STRING, dot_string))
                    return None

        for fieldname in fields:
            add_to_fields_map(self.model, fields_map, fieldname)

        if ERROR_STRING in self.error:
            fields_map = {}
        return fields_map

    def _set_relateds(self, fields_map):
        """Go through the fields_map and see what related objs should be added to the querystring"""
        def add_to_relateds(current_model, current_map, fieldname, current_string=''):
            # figure out if the field should be a prefetch or select, and add it. Also validate against drilldowns
            if current_map[fieldname]:  # e.g. if there are sub-fields
                field_type = get_field_type(current_model, fieldname)
                current_string = (current_string + '__' + fieldname).strip('__')
                if field_type in [ForeignKey, OneToOneField, RelatedObject, ManyToManyField]:
                    if not current_string in self.drilldowns:
                        self.error = ('Error: %s not valid' % current_string.replace('__', '.'))
                        return None
                    if field_type in [ForeignKey, OneToOneField, RelatedObject]:
                        self.select_relateds.append(current_string)
                    else:
                        self.prefetch_relateds.append(current_string)

                new_model = get_model(current_model, fieldname)
                for f in current_map.get(fieldname, {}):
                    add_to_relateds(new_model, current_map[fieldname], f, current_string)  # recursion

        for fieldname in fields_map:
            add_to_relateds(self.model, fields_map, fieldname)
        return True

    def _set_filter_kwargs(self, filters):
        """Create the kwargs to filter the querystring with"""
        filter_kwargs = {}
        for p in filters:
            pair = p.split('__')
            dot_string = pair[0]
            if len(pair) > 1:
                operation = '__' + pair[1]  # 'operation' is something like '__gt', '__isnull', etc.
            else:
                operation = ''

            def do_filter(dot_string, filter_string, current_model):
                """
                Recursive function that takes 'invoice.client.last_name'
                and puts out a string like 'invoice__client__last_name' after validating that all the fields exist
                """
                parts = dot_string.split('.', 1)
                fieldname = parts[0]
                filter_string = (filter_string + '__' + fieldname).strip('__')
                if len(parts) > 1:
                    leftover = parts[1]
                else:
                    leftover = ''

                if not is_field_in(current_model, fieldname):
                    self.error = ('"%s" is not a valid filter' % fieldname)
                    return None

                if leftover:
                    field_type = get_field_type(current_model, fieldname)
                    if filter_string not in self.drilldowns:
                        self.error = 'Error in filters: %s' % filter_string.replace('__', '.')
                        return None
                    if field_type not in [ForeignKey, OneToOneField, RelatedObject, ManyToManyField]:
                        self.error = ('Error: %s has no children' % filter_string)
                        return None

                    # go to the related model
                    current_model = get_model(current_model, fieldname)
                    return do_filter(leftover, filter_string, current_model)  # recursion
                else:
                    return filter_string

            filter_string = do_filter(dot_string, '', self.model)

            if filter_string:
                filter_kwargs[filter_string + operation] = self.request.QUERY_PARAMS[p]

        for k in filter_kwargs:
            if filter_kwargs[k] in ['true', 'True']:
                filter_kwargs[k] = True
            elif filter_kwargs[k] in ['false', 'False']:
                filter_kwargs[k] = False

        return filter_kwargs


def DrilldownSerializerFactory(the_model):
    """Creates a generic model serializer with sub-serializers, based on the fields map """
    class Serializer(serializers.ModelSerializer):
        class Meta:
            model = the_model

        def __init__(self, *args, **kwargs):
            # pull off the fields_map argument; don't pass to superclass
            if 'fields_map' in kwargs:
                fields_map = kwargs.pop('fields_map')
                fields_map = fields_map
            else:
                fields_map = {}

            super(Serializer, self).__init__(*args, **kwargs)

            if fields_map:
                # recurse through the fields dict, setting the fields list for each level and building sub-serializers
                def prune_fields(fields_map, model):
                    # Set the list of fields for this serializer
                    requested = list(fields_map)  # flatten to get fields requested for this specific serializer model
                    available = list(self.fields)  # by default this is all fields for the model
                    for field_name in set(available) - set(requested):
                        self.fields.pop(field_name)  # delete the ones we don't want from the serializer
                        if field_name in fields_map:  # and from fields_map
                            del fields_map[field_name]
                        # Attach sub-serializers for relationship fields
                    for field_name in fields_map:
                        sub_fm = fields_map[field_name]
                        if sub_fm and sub_fm != {'id': {}}:  # only do this for fields with sub-fields requested
                            ftype = get_field_type(model, field_name)
                            if ftype in [ForeignKey, OneToOneField, RelatedObject, ManyToManyField]:
                                m = get_model(model, field_name)
                                self.fields[field_name] = DrilldownSerializerFactory(m)(
                                    fields_map=fields_map[field_name])  # recursively create another serializer

                prune_fields(fields_map=fields_map, model=self.Meta.model)
            else:
                # if no fields specified, return ids only
                for field_name in set(self.fields):
                    if field_name != 'id':
                        self.fields.pop(field_name)
    return Serializer


# Some utilities
def get_model(parent_model, fieldname):
    """Get the model of a foreignkey, manytomany, etc. field"""
    field_type = type(parent_model._meta.get_field_by_name(fieldname)[0])
    if field_type in [ForeignKey, ManyToManyField, OneToOneField]:
        model = parent_model._meta.get_field(fieldname).rel.to
    elif field_type == RelatedObject:
        model = parent_model._meta.get_field_by_name(fieldname)[0].model
    else:
        model = None
    return model


def get_field_type(model, fieldname):
    """Get the type of a field in a model"""
    return type(model._meta.get_field_by_name(fieldname)[0])


def is_field_in(model, fieldname):
    """Return true if fieldname is a field or relatedobject in model"""
    fieldnames = model._meta.get_all_field_names()
    return fieldname in fieldnames


def int_or_none(value):
    """Convenience method to return None if int fails"""
    try:
        result = int(value)
    except (ValueError, TypeError):
        result = None
    return result
