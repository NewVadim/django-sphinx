#coding: utf-8
from __future__ import unicode_literals

__author__ = 'ego'

import MySQLdb
import re
import time
import warnings

import six

try:
    from collections import OrderedDict
except ImportError:
    from django.utils.datastructures import SortedDict as OrderedDict # Python < 2.7

from datetime import datetime, date

try:
    import decimal
except ImportError:
    from django.utils import _decimal as decimal  # for Python 2.3

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.fields.related import RelatedField
from django.db.models.query import QuerySet
from django.utils.encoding import force_unicode

from djangosphinx.conf import SPHINX_QUERY_OPTS, SPHINX_QUERY_LIMIT, \
    SPHINX_MAX_MATCHES, SPHINX_SNIPPETS, SPHINX_SNIPPETS_OPTS, \
    DOCUMENT_ID_SHIFT, CONTENT_TYPE_MASK, OBJECT_ID_MASK

from djangosphinx.constants import EMPTY_RESULT_SET, \
    FILTER_CMP_OPERATIONS, FILTER_CMP_INVERSE

from djangosphinx.query.proxy import SphinxProxy
from djangosphinx.query.query import SphinxQuery, conn_handler
from djangosphinx.utils.config import get_sphinx_attr_type_for_field
from djangosphinx.shortcuts import all_indexes

__all__ = ['SearchError', 'SphinxQuerySet', 'to_sphinx']


def to_sphinx(value):
    "Convert a value into a sphinx query value"
    if isinstance(value, (date, datetime)):
       return int(time.mktime(value.timetuple()))
    elif isinstance(value, (decimal.Decimal, float)):
       return float(value)
    return int(value)


class SearchError(Exception):
    pass


class SphinxQuerySet(object):

    __index_match = re.compile(r'[^a-z0-9_-]*', re.I)

    def __init__(self, model=None, using=None, **kwargs):
        self.model = model
        self.using = using
        self.realtime = None
        self._doc_ids = None

        self._iter = None

        self._query = None
        self._query_args = None

        self._field_names = {}
        self._fields = '*'
        self._aliases = {}
        self._group_by = ''
        self._order_by = ''
        self._group_order_by = ''

        self._filters = {}
        self._excludes = {}

        _q_opts = kwargs.pop('query_options', SPHINX_QUERY_OPTS)
        if 'ranker' not in _q_opts:
            _q_opts['ranker'] = 'bm25'

        self._query_opts = self._format_options(**_q_opts)

        self._result_cache = None
        self._doc_fields_cache = {}
        self._index_fields_cache = None
        self._metadata = None

        self._maxmatches = min(kwargs.pop('maxmatches', SPHINX_MAX_MATCHES), SPHINX_MAX_MATCHES)

        self._limit = min(kwargs.pop('limit', SPHINX_QUERY_LIMIT), self._maxmatches)
        self._offset = None

        self._snippets = kwargs.pop('snippets', SPHINX_SNIPPETS)
        self._snippets_opts = kwargs.pop('snippets_options', SPHINX_SNIPPETS_OPTS)
        self._snippets_string = None

        if model:
            #self._indexes = self._parse_indexes(kwargs.pop('index', model._meta.db_table))
            self._indexes = [model._meta.db_table]
            model_options = model.__sphinx_options__
            if model_options.get('realtime', False):
                self.realtime = '%s_rt' % model._meta.db_table
                self._indexes.append(self.realtime)
        else:
            self._indexes = self._parse_indexes(kwargs.pop('index', None))

    def __len__(self):
        return self.count()

    def __iter__(self):
        if self._result_cache is None:
            try:
                self._get_data()
            except MySQLdb.ProgrammingError as e:
                raise SearchError(e.args)

        return iter(self._result_cache)

    def __repr__(self):
        return repr(self.__iter__())

    def __getitem__(self, k):
        """
        Retrieves an item or slice from the set of results.
        """
        if not isinstance(k, (slice,) + six.integer_types):
            raise TypeError
        assert ((not isinstance(k, slice) and (k >= 0))
                or (isinstance(k, slice) and (k.start is None or k.start >= 0)
                    and (k.stop is None or k.stop >= 0))),\
            "Negative indexing is not supported."

        if isinstance(k, slice):
            qs = self._clone()
            start = int(k.start) if k.start is not None else 0
            stop = int(k.stop) if k.stop is not None else None

            qs._set_limits(start, stop)
            qs._get_data()
            return k.step and list(qs)[::k.step] or qs

        try:
            qs = self._clone()
            qs._set_limits(k, k + 1)
            qs._get_data()
            return list(qs)[0]
        except Exception as e:
            raise IndexError(e.args)

    # Indexes

    def add_index(self, index):
        if self.model is not None:
            raise SearchError('You can not add an index to the model')

        _indexes = self._indexes[:]

        for x in self._parse_indexes(index):
            if x not in _indexes:
                _indexes.append(x)

        return self._clone(_indexes=_indexes)

    def remove_index(self, index):
        if self.model is not None:
            raise SearchError('You can not remove an index from model')

        _indexes = self._indexes[:]

        for x in self._parse_indexes(index):
            if x in _indexes:
                _indexes.pop(_indexes.index(x))

        return self._clone(_indexes=_indexes)

    # Querying

    def query(self, query):
        return self._clone(_query=force_unicode(query))

    def filter(self, **kwargs):
        filters = self._filters.copy()
        return self._clone(_filters=self._process_filters(filters, False, **kwargs))

    def exclude(self, **kwargs):
        filters = self._excludes.copy()
        return self._clone(_excludes=self._process_filters(filters, True, **kwargs))

    def fields(self, *args, **kwargs):
        fields = ''
        aliases = {}
        if args:
            fields = '`%s`' % '`, `'.join(args)
        if kwargs:
            for k, v in kwargs.iteritems():
                aliases[k] = '%s AS `%s`' % (v, k)

        if fields or aliases:
            return self._clone(_fields=fields, _aliases=aliases)
        return self

    def options(self, **kwargs):
        if not kwargs:
            return self
        return self._clone(_query_opts=self._format_options(**kwargs))

    def snippets(self, snippets=True, **kwargs):
        if snippets == self._snippets and not kwargs:
            return self

        for k, v in kwargs.iteritems():
            if isinstance(v, bool):
                v = int(v)

        return self._clone(_snippets_opts=kwargs, _snippets=snippets, _snippets_opts_string=None)

    # Currently only supports grouping by a single column.
    # The column however can be a computed expression
    def group_by(self, field):
        return self._clone(_group_by='GROUP BY `%s`' % field)

    def order_by(self, *args):
        sort_by = []
        for arg in args:
            order = 'ASC'
            if arg[0] == '-':
                order = 'DESC'
                arg = arg[1:]
            if arg == 'pk':
                arg = 'id'

            sort_by.append('`%s` %s' % (arg, order))

        if sort_by:
            return self._clone(_order_by='ORDER BY %s' % ', '.join(sort_by))
        return self

    def group_order_by(self, *args):
        sort_by = []
        for arg in args:
            order = 'ASC'
            if arg[0] == '-':
                order = 'DESC'
                arg = arg[1:]
            if arg == 'pk':
                arg = 'id'

            sort_by.append('`%s` %s' % (arg, order))

        if sort_by:
            return self._clone(_group_order_by='WITHIN GROUP ORDER BY %s' % ', '.join(sort_by))
        return self

    def count(self):
        return min(int(self.meta.get('total_found', 0)), self._maxmatches)

    # Возвращяет все объекты из индекса. Размер списка ограничен только
    # значением maxmatches
    def all(self):
        return self._clone(_limit=self._maxmatches, _offset=None)

    def none(self):
        qs = EmptySphinxQuerySet()
        qs.__dict__.update(self.__dict__.copy())
        return qs

    def reset(self):
        return self.__class__(self.model, self.using, index=self._get_index())

    def _get_values_for_update(self, obj):
        fields = self._get_index_fields()
        values = []
        for field in fields[:]:
            if field == 'id':
                f = getattr(obj, 'pk')
                f = self._encode_document_id(f)
            else:
                f = getattr(obj, field)

                if hasattr(f, 'through'): # ManyToMany
                    # пропускаем пока что...
                    f = [force_unicode(x.pk) for x in f.all()]
                elif isinstance(f, six.string_types):
                    pass
                elif isinstance(f, six.integer_types) or isinstance(f, (bool, date, datetime, float, decimal.Decimal)):
                    f = to_sphinx(f)
                else:
                    model_filed = obj._meta.get_field(field)
                    if isinstance(model_filed, RelatedField):
                        f = to_sphinx(getattr(obj, model_filed.column))
                    elif f is None:
                        if isinstance(model_filed, (
                                models.TextField, models.CharField, models.FileField,
                                models.FilePathField, models.IPAddressField, models.GenericIPAddressField
                        )):
                            f = ''
                        elif isinstance(model_filed, (
                                models.IntegerField, models.BooleanField, models.NullBooleanField,
                                models.DateField, models.FloatField, models.BinaryField, models.TimeField
                        )):
                            f = 0
                        elif isinstance(model_filed, (models.FloatField, models.DecimalField)):
                            f = 0.0
                    else:
                        raise SearchError('Unknown field `%s`' % type(f))

            values.append(f)

        return values

    def create(self, *args, **kwargs):
        values = ()

        if self.model:
            assert len(args) == 1, \
                    'Model RT-index can be updated by object instance or queryset'
            obj = args[0]
            if isinstance(obj, self.model):
                # один объект, один документ
                values = (self._get_values_for_update(obj),)
            elif isinstance(obj, QuerySet):
                # несколько объектов, несколько документов
                values = map(self._get_values_for_update, obj)
            else:
                raise SearchError('Can`t `%s` not an instance/queryset of `%s`' % (obj, self.model))
        else:
            raise NotImplementedError('Non-model RT-index update not supported yet')

        if not values:
            raise SearchError('Empty QuerySet? o_O')

        query = ['REPLACE' if kwargs.pop('force_update', False) else 'INSERT']
        query.append('INTO %s' % self.realtime)
        query.append('(%s)' % ','.join(self._get_index_fields()))
        query.append('VALUES')

        query_args = []
        q = []
        for v in values:
            f_list = []
            for f in v:
                if isinstance(f, six.string_types):
                    query_args.append(f)
                    f_list.append('%s')
                elif isinstance(f, (list, tuple)):
                    f_list.append('(%s)' % ','.join(f))
                else:
                    f_list.append(force_unicode(f))

            q.append('(%s)' % ','.join(f_list))

        query.append(', '.join(q))

        cursor = conn_handler.cursor()
        count = cursor.execute(' '.join(query), query_args)

        return count

    def update(self, **kwargs):
        raise NotImplementedError('Update not implemented yet')

    def delete(self):
        """
        Удаляет из индекса документы, удовлетворяющие условиям filter
        """

        assert self._can_modify(),\
                "Cannot use 'limit' or 'offset' with delete."

        q = ['DELETE FROM %s WHERE' % self.realtime]

        if len(self._doc_ids) == 1:
            where = 'id = %i' % self._doc_ids[0]
        else:
            where = 'id IN (%s)' % ','.join(str(id) for id in self._doc_ids)

        q.append(where)

        query = ' '.join(q)

        cursor = conn_handler.cursor()
        cursor.execute(query, self._query_args)

    # misc
    def keywords(self, text, index=None, hits=None):
        """\
        Возвращает генератор со списком ключевых слов
        для переданного текста\
        """
        if index is None:
            # пока только для одного индекса
            index = self._indexes[0]

        query = 'CALL KEYWORDS (%s)'
        q = ['%s', '%s']
        if hits is not None and hits:
            q.append('1')

        query = query % ', '.join(q)

        cursor = conn_handler.cursor()
        count = cursor.execute(query, [text, index])

        for x in range(0, count):
            yield cursor.fetchone()

    def get_query_set(self, model):
        qs = model._default_manager
        if self.using is not None:
            qs = qs.db_manager(self.using)
        return qs.all()

    # Properties

    def _meta(self):
        if self._metadata is None:
            self._get_data()

        return self._metadata

    meta = property(_meta)

    def _get_snippets_string(self):
        if self._snippets_string is None:
            opts_list = []
            for k, v in self._snippets_opts.iteritems():
                opt = ('\'%s\' AS %s' if isinstance(v, six.string_types) else '%s AS %s') % (v, k)
                opts_list.append(opt)

            if opts_list:
                self._snippets_string = ', %s' % ', '.join(opts_list)

        return self._snippets_string or ''

    #internal


    def _set_limits(self, start, stop=None):
        if start is not None:
            self._offset = int(start)
        else:
            start = 0

        if stop is not None:
            self._limit = stop - start

    def _can_modify(self):
        if self.realtime is None:
            raise SearchError('Documents can`t be modified on the non-realtime index')

        assert self._doc_ids is not None \
               and not self._excludes and self._query is None\
               and len(self._filters) == 1 and 'id' in self._filters, \
                'Only {id = value | id IN (val1 [, val2 [, ...]])} filters allowed here'

        return self._offset is None

    def _get_data(self):
        if not self._indexes:
            #warnings.warn('Index list is not set. Using all known indices.')
            self._indexes = self._parse_indexes(all_indexes())

        self._iter = SphinxQuery(self.query_string, self._query_args)
        self._result_cache = []
        self._metadata = self._iter.meta
        self._fill_cache()

    ## Options
    def _parse_indexes(self, index):
        if index is None:
            return list()

        return [x.lower() for x in re.split(self.__index_match, index) if x]

    def _get_index(self):
        return ' '.join(self._indexes)

    def _format_options_dict(self, d):
        return '(%s)' % ', '.join(['%s=%s' % (x, d[x]) for x in d])

    def _format_options(self, **kwargs):
        if not kwargs:
            return ''
        opts = []
        for k, v in kwargs.iteritems():
            if isinstance(v, bool):
                v = int(v)
            elif isinstance(v, dict):
                v = self._format_options_dict(v)

            opts.append('%s=%s' % (k, v))

        return 'OPTION %s' % ','.join(opts)

    ## Cache

    def _fill_cache(self, num=None):
        fields = self.meta['fields'].copy()
        id_pos = fields.pop('id')
        ct = None
        results = {}

        docs = OrderedDict()

        if self._iter:
            try:
                while True:
                    doc = self._iter.next()
                    doc_id = doc[id_pos]

                    obj_id, ct = self._decode_document_id(int(doc_id))

                    results.setdefault(ct, {})[obj_id] = {}

                    docs.setdefault(doc_id, {})['results'] = results[ct][obj_id]
                    docs[doc_id]['data'] = {}

                    for field in fields:
                        docs[doc_id]['data'].setdefault('fields', {})[field] = doc[fields[field]]
            except StopIteration:
                self._iter = None
                if not docs:
                    self._result_cache = []
                    return

                if self.model is None and len(self._indexes) == 1 and ct is not None:
                    self.model = ContentType.objects.get(pk=ct).model_class()

                if self.model:
                    qs = self.get_query_set(self.model)

                    qs = qs.filter(pk__in=results[ct].keys())

                    for obj in qs:
                        results[ct][obj.pk]['obj'] = obj

                else:
                    for ct in results:
                        model_class = ContentType.objects.get(pk=ct).model_class()
                        qs = self.get_query_set(model_class).filter(pk__in=results[ct].keys())

                        for obj in qs:
                            results[ct][obj.pk]['obj'] = obj
                #clear missing items
                for pk in [pk for pk, doc in docs.items() if not 'obj' in doc['results']]:
                    del docs[pk]

                if self._snippets:
                    for doc in docs.values():
                        doc['data']['snippets'] = self._get_snippets(doc['results']['obj'])
                        self._result_cache.append(SphinxProxy(doc['results']['obj'], doc['data']))
                else:
                    for doc in docs.values():
                        self._result_cache.append(SphinxProxy(doc['results']['obj'], doc['data']))


    ## Snippets
    def _get_snippets(self, instance):
        (fields, docs) = zip(*[(f, getattr(instance, f)) for f in self._get_doc_fields(instance) if getattr(instance, f)])

        opts = self._get_snippets_string()

        doc_format = ', '.join('%s' for x in range(0, len(fields)))
        query = 'CALL SNIPPETS (({0:>s}), \'{1:>s}\', %s {2:>s})'.format(doc_format,
            instance.__sphinx_indexes__[0],
            opts)
        docs += (self._query or '',)

        c = conn_handler.cursor()
        c.execute(query, docs)

        snippets = {}
        for field in fields:
            snippets[field] = c.fetchone()[0].decode('utf-8')

        return snippets

    def _get_doc_fields(self, instance):
        cache = self._doc_fields_cache.get(type(instance), None)
        if cache is None:
            def _get_field(name):
                return instance._meta.get_field(name)

            opts = instance.__sphinx_options__
            included = opts.get('included_fields', [])
            excluded = opts.get('excluded_fields', [])
            stored_attrs = opts.get('stored_attributes', [])
            stored_fields = opts.get('stored_fields', [])
            if included:
                included = [f for f in included if
                            f not in excluded
                            and
                            get_sphinx_attr_type_for_field(_get_field(f)) == 'string']
                for f in stored_fields:
                    if get_sphinx_attr_type_for_field(_get_field(f)) == 'string':
                        included.append(f)
            else:
                included = [f.name for f in instance._meta.fields
                            if
                            f.name not in excluded
                            and
                            (f.name not in stored_attrs
                             or
                             f.name in stored_fields)
                            and
                            get_sphinx_attr_type_for_field(f) == 'string']

            cache = self._doc_fields_cache[type(instance)] = included

        return cache

    def _get_index_fields(self):
        if self._index_fields_cache is None:
            opts = self.model.__sphinx_options__

            excluded = opts.get('excluded_fields', [])

            fields = []
            for f in ['included_fields', 'stored_attributes',
                      'stored_fields', 'related_fields', 'mva_fields']:
                fields.extend(opts.get(f, []))
            for f in excluded:
                if f in fields:
                    fields.pop(fields.index(f))

            fields.insert(0, 'id')

            self._index_fields_cache = fields

        return self._index_fields_cache

    ## Documents
    def _decode_document_id(self, doc_id):
        """\
        Декодирует ID документа, полученного от Sphinx

        :param doc_id: ID документа
        :type doc_id: long

        :returns: tuple(ContentTypeID, ObjectID)
        :rtype: tuple\
        """
        assert isinstance(doc_id, six.integer_types)

        ct = (doc_id & CONTENT_TYPE_MASK) >> DOCUMENT_ID_SHIFT
        return (doc_id & OBJECT_ID_MASK, ct)

    def _encode_document_id(self, id):
        if self.model:
            ct = ContentType.objects.get_for_model(self.model)

            id = int(ct.id) << DOCUMENT_ID_SHIFT | id

        return id


    ## Filters
    def _process_single_obj_operation(self, obj):
        if isinstance(obj, models.Model):
            if self.model is None:
                raise ValueError('For non model or multiple model indexes comparsion with objects not supported')
            value = obj.pk
        elif not isinstance(obj, (list, tuple, QuerySet)):
            value = obj
        else:
            raise TypeError('Comparison operations require a single object, not a `%s`' % type(obj))

        return to_sphinx(value)

    def _process_obj_list_operation(self, obj_list):
        if isinstance(obj_list, (models.Model, QuerySet)):
            if self.model is None:
                raise ValueError('For non model or multiple model indexes comparsion with objects not supported')

            if isinstance(obj_list, models.Model):
                values = [obj_list.pk]
            else:
                values = [obj.pk for obj in obj_list]

        elif hasattr(obj_list, '__iter__') or isinstance(obj_list, (list, tuple)):
            values = list(obj_list)
        elif isinstance(obj_list, (int, float, date, datetime)):
            values = [obj_list]
        else:
            raise ValueError('`%s` is not a list of objects and not single object' % type(obj_list))

        return map(to_sphinx, values)

    def _process_filters(self, filters, exclude=False, **kwargs):
        for k, v in kwargs.iteritems():
            if  len(k.split('__')) > 3:
                raise NotImplementedError('Related model fields lookup not supported')

            parts = k.rsplit('__', 1)
            parts_len = len(parts)
            field = parts[0]
            lookup = parts[-1]

            if field == 'pk': # приводим pk к id
                field = 'id'

            if parts_len == 1:  # один
                if field == 'id':
                    v = self._encode_document_id(self._process_single_obj_operation(v))
                    self._doc_ids = [v]
                else:
                    v = self._process_single_obj_operation(v)

                filters[field] = '%s %s %s' % (field,
                                             '!=' if exclude else '=',
                                             v)
            elif parts_len == 2: # один exact или список, или сравнение
                if lookup == 'in':
                    if field == 'id':
                        v = map(self._encode_document_id, self._process_obj_list_operation(v))
                        self._doc_ids = v
                    else:
                        v = self._process_obj_list_operation(v)

                    filters[field] = '%s %sIN (%s)' % (field,
                                                     'NOT ' if exclude else '',
                                                     ','.join(str(x) for x in v))
                elif lookup == 'range':
                    v = self._process_obj_list_operation(v)
                    if len(v) != 2:
                        raise ValueError('Range may consist of two values')
                    if exclude:
                        # not supported by sphinx. raises error!
                        warnings.warn('Exclude range not supported by SphinxQL now!')
                        filters[field] = 'NOT %s BETWEEN %s AND %s' % (field, v[0], v[1])
                    else:
                        filters[field] = '%s BETWEEN %s AND %s' % (field, v[0], v[1])

                elif lookup in FILTER_CMP_OPERATIONS:
                    filters[field] = '%s %s %s' % (field,
                                                 FILTER_CMP_INVERSE[lookup]\
                                                 if exclude\
                                                 else FILTER_CMP_OPERATIONS[lookup],
                                                 self._process_single_obj_operation(v))
                else:  # stored related field
                    filters[k] = '%s %s %s' % (k,
                                             '!=' if exclude else '=',
                                             self._process_single_obj_operation(v))


        return filters


    ## Query
    def _build_query(self):
        self._query_args = []

        q = ['SELECT']

        q.extend(self._build_fields())

        q.extend(['FROM', ', '.join(self._indexes)])

        q.extend(self._build_where())

        q.append(self._build_group_by())
        q.append(self._build_order_by())
        q.append(self._build_group_order_by())

        q.extend(self._build_limits())

        if self._query_opts is not None:
            q.append(self._query_opts)

        return ' '.join(q)

    query_string = property(_build_query)

    def _build_fields(self):
        q = []
        if self._fields:
            q.append(self._fields)
            if self._aliases:
                q.append(',')

        if self._aliases:
            q.append(', '.join(self._aliases.values()))
        return q

    def _build_where(self):
        q = []
        if self._query or self._filters or self._excludes:
            q.append('WHERE')
        if self._query:
            q.append('MATCH(%s)')
            self._query_args.append(self._query)

            if self._filters or self._excludes:
                q.append('AND')
        if self._filters:
            q.append(' AND '.join(self._filters.values()))
            if self._excludes:
                q.append('AND')
        if self._excludes:
            q.append(' AND '.join(self._excludes.values()))

        return q

    def _build_group_by(self):
        return self._group_by

    def _build_order_by(self):
        return self._order_by

    def _build_group_order_by(self):
        return self._group_order_by

    def _build_limits(self):
        if not self._limit is None and self._offset is None:
            return ''

        q = ['LIMIT']
        if self._offset is not None:
            q.append('%i,' % self._offset)
        q.append('%i' % (self._limit if self._limit is not None else self._maxmatches))

        return q

    ## Clone
    def _clone(self, **kwargs):
        """\
        Clones the queryset passing any changed args\
        """
        c = self.__class__()
        c.__dict__.update(self.__dict__.copy())

        c._result_cache = None
        c._metadata = None
        c._iter = None

        for k, v in kwargs.iteritems():
            setattr(c, k, v)

        return c


class EmptySphinxQuerySet(SphinxQuerySet):
    def _get_data(self):
        self._iter = iter([])
        self._result_cache = []
        self._metadata = EMPTY_RESULT_SET
