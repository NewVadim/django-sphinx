# coding: utf-8
from __future__ import unicode_literals, absolute_import
from _mysql import OperationalError, ProgrammingError

import warnings
from django.db import models
from django.db.models.query import QuerySet

from .query import SphinxQuerySet, SearchError


class SphinxModelManager(object):
    def __init__(self, model, **kwargs):
        self.model = model
        self._index = kwargs.pop('index', model._meta.db_table)
        self._kwargs = kwargs

    def _get_query_set(self):
        return SphinxQuerySet(self.model, index=self._index, **self._kwargs)

    def get_index(self):
        return self._index

    def all(self):
        return self._get_query_set()

    def none(self):
        return self._get_query_set().none()

    def filter(self, **kwargs):
        return self._get_query_set().filter(**kwargs)

    def query(self, *args, **kwargs):
        return self._get_query_set().query(*args, **kwargs)

    def create(self, *args, **kwargs):
        return self._get_query_set().create(*args, **kwargs)

    def update(self, **kwargs):
        return self._get_query_set().update(**kwargs)

    def delete(self):
        return self._get_query_set().delete()


class SphinxSearch(object):
    def __init__(self, index=None, using=None, **kwargs):
        # Metadata for things like excluded_fields, included_fields, etc

        self._options = kwargs.pop('options', dict())

        self._kwargs = kwargs
        self._sphinx = None
        self._index = index

        self.model = None
        self.using = using

    def __call__(self, index, **kwargs):
        warnings.warn('For non-model searches use a SphinxQuerySet instance.', DeprecationWarning)
        return SphinxQuerySet(index=index, using=self.using, **kwargs)

    def get_query_set(self):
        """Override this method to change the QuerySet used for config generation."""
        return self.model._default_manager.all()

    def contribute_to_class(self, model, name, **kwargs):
        if self._index is None:
            self._index = model._meta.db_table
        self._sphinx = SphinxModelManager(model, index=self._index, **self._kwargs)
        self.model = model

        if hasattr(model, '__sphinx_indexes__') or hasattr(model, '__sphinx_options__'):
            raise AttributeError('Only one instance of SphinxSearch can be present in the model: `%s.%s`' % (self.model._meta.app_label, self.model._meta.object_name))

        setattr(model, '__sphinx_indexes__', [self._index])
        setattr(model, '__sphinx_options__', self._options)

        setattr(model, name, self._sphinx)


class RTQuerySet(QuerySet):
    def delete(self, return_ids=False):
        pk_list = self.values_list('pk', flat=True)
        self.model.search.filter(id__in=list(pk_list)).delete()
        super(RTQuerySet, self).delete()

        if return_ids:
            return pk_list


class RTManager(models.Manager):
    def get_queryset(self):
        return RTQuerySet(self.model, using=self._db)


class RTAbstractModel(models.Model):
    class Meta:
        abstract = True

    objects = RTManager()

    def save(self, *args, **kwargs):
        self.rt_index_create_or_update()
        super(RTAbstractModel, self).save(*args, **kwargs)

    def rt_index_create_or_update(self):
        try:
            if self.id:
                self.search.create(self, force_update=True)
            else:
                self.search.create(self)

        except ProgrammingError:
            warnings.warn(
                "The old object, but index didn't exist, the index was created!",
                Warning
            )
            self.search.create(self)

    def delete(self, *args, **kwargs):
        pk = self.pk
        super(RTAbstractModel, self).delete(*args, **kwargs)
        self.search.filter(id=pk).delete()