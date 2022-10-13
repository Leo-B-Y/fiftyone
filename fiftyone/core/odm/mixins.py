"""
Mixins and helpers for dataset backing documents.

| Copyright 2017-2022, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from collections import OrderedDict

from bson import ObjectId

import fiftyone.core.fields as fof
import fiftyone.core.media as fom
import fiftyone.core.utils as fou

from .database import get_db_conn
from .dataset import create_field, SampleFieldDocument
from .document import Document
from .utils import (
    serialize_value,
    deserialize_value,
    validate_field_name,
    get_implied_field_kwargs,
    validate_fields_match,
)

fod = fou.lazy_import("fiftyone.core.dataset")
fog = fou.lazy_import("fiftyone.core.groups")


def get_default_fields(cls, include_private=False, use_db_fields=False):
    """Gets the default fields present on all instances of the given
    :class:`DatasetMixin` class.

    Args:
        cls: the :class:`DatasetMixin` class
        include_private (False): whether to include fields starting with ``_``
        use_db_fields (False): whether to return database fields rather than
            user-facing fields, when applicable

    Returns:
        a tuple of field names
    """
    return cls._get_fields_ordered(
        include_private=include_private, use_db_fields=use_db_fields
    )


class DatasetMixin(object):
    """Mixin interface for :class:`fiftyone.core.odm.document.Document`
    subclasses that are backed by a dataset.
    """

    # Subtypes must declare this
    _is_frames_doc = None

    def __setattr__(self, name, value):
        if name in self._fields and value is not None:
            self._fields[name].validate(value)

        super().__setattr__(name, value)

    @property
    def collection_name(self):
        return self.__class__.__name__

    @property
    def field_names(self):
        return self._get_field_names(include_private=False)

    @classmethod
    def _doc_name(cls):
        return "Frame" if cls._is_frames_doc else "Sample"

    @classmethod
    def _fields_attr(cls):
        return "frame_fields" if cls._is_frames_doc else "sample_fields"

    @classmethod
    def _dataset_doc(cls):
        collection_name = cls.__name__
        return fod._get_dataset_doc(collection_name, frames=cls._is_frames_doc)

    def _get_field_names(self, include_private=False, use_db_fields=False):
        return self._get_fields_ordered(
            include_private=include_private,
            use_db_fields=use_db_fields,
        )

    def has_field(self, field_name):
        # pylint: disable=no-member
        return field_name in self._fields

    def get_field(self, field_name):
        if not self.has_field(field_name):
            raise AttributeError(
                "%s has no field '%s'" % (self._doc_name(), field_name)
            )

        return super().get_field(field_name)

    def set_field(
        self,
        field_name,
        value,
        create=True,
        validate=True,
        dynamic=False,
    ):
        validate_field_name(field_name)

        if not self.has_field(field_name):
            if create:
                self.add_implied_field(
                    field_name,
                    value,
                    expand_schema=True,
                    validate=validate,
                    dynamic=dynamic,
                )
            else:
                raise ValueError(
                    "%s has no field '%s'" % (self._doc_name(), field_name)
                )
        elif value is not None:
            if validate:
                self._fields[field_name].validate(value)

            if dynamic:
                self.add_implied_field(
                    field_name,
                    value,
                    expand_schema=create,
                    validate=validate,
                    dynamic=dynamic,
                )

        super().__setattr__(field_name, value)

    def clear_field(self, field_name):
        self.set_field(field_name, None, create=False)

    @classmethod
    def get_field_schema(
        cls, ftype=None, embedded_doc_type=None, include_private=False
    ):
        """Returns a schema dictionary describing the fields of this document.

        If the document belongs to a dataset, the schema will apply to all
        documents in the collection.

        Args:
            ftype (None): an optional field type to which to restrict the
                returned schema. Must be a subclass of
                :class:`fiftyone.core.fields.Field`
            embedded_doc_type (None): an optional embedded document type to
                which to restrict the returned schema. Must be a subclass of
                :class:`fiftyone.core.odm.BaseEmbeddedDocument`
            include_private (False): whether to include fields that start with
                ``_`` in the returned schema

        Returns:
             a dictionary mapping field names to field types
        """
        fof.validate_type_constraints(
            ftype=ftype, embedded_doc_type=embedded_doc_type
        )

        schema = OrderedDict()
        field_names = cls._get_fields_ordered(include_private=include_private)
        for field_name in field_names:
            # pylint: disable=no-member
            field = cls._fields[field_name]

            if fof.matches_type_constraints(
                field, ftype=ftype, embedded_doc_type=embedded_doc_type
            ):
                schema[field_name] = field

        return schema

    @classmethod
    def merge_field_schema(
        cls,
        schema,
        expand_schema=True,
        recursive=True,
        validate=True,
        dataset_doc=None,
    ):
        """Merges the field schema into this document.

        Args:
            schema: a dictionary mapping field names or
                ``embedded.field.names``to
                :class:`fiftyone.core.fields.Field` instances
            expand_schema (True): whether to add new fields to the schema
                (True) or simply validate that fields already exist with
                consistent types (False)
            recursive (True): whether to recursively merge embedded document
                fields
            validate (True): whether to validate the field against an existing
                field at the same path
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`

        Returns:
            True/False whether any new fields were added

        Raises:
            ValueError: if a field in the schema is not compliant with an
                existing field of the same name or a new field is found but
                ``expand_schema == False``
        """
        if dataset_doc is None:
            dataset_doc = cls._dataset_doc()

        new_schema = {}

        for path, field in schema.items():
            new_fields = cls._merge_field(
                path,
                field,
                dataset_doc,
                validate=validate,
                recursive=recursive,
            )
            if new_fields:
                new_schema.update(new_fields)

        if new_schema and not expand_schema:
            raise ValueError(
                "%s fields %s do not exist"
                % (cls._doc_name(), list(new_schema.keys()))
            )

        if not new_schema:
            return False

        for path, field in new_schema.items():
            cls._add_field_schema(path, field, dataset_doc)

        dataset_doc.save()

        return True

    @classmethod
    def add_field(
        cls,
        path,
        ftype,
        embedded_doc_type=None,
        subfield=None,
        fields=None,
        expand_schema=True,
        recursive=True,
        validate=True,
        dataset_doc=None,
        **kwargs,
    ):
        """Adds a new field or embedded field to the document, if necessary.

        Args:
            path: the field name or ``embedded.field.name``
            ftype: the field type to create. Must be a subclass of
                :class:`fiftyone.core.fields.Field`
            embedded_doc_type (None): the
                :class:`fiftyone.core.odm.BaseEmbeddedDocument` type of the
                field. Only applicable when ``ftype`` is
                :class:`fiftyone.core.fields.EmbeddedDocumentField`
            subfield (None): the :class:`fiftyone.core.fields.Field` type of
                the contained field. Only applicable when ``ftype`` is
                :class:`fiftyone.core.fields.ListField` or
                :class:`fiftyone.core.fields.DictField`
            fields (None): a list of :class:`fiftyone.core.fields.Field`
                instances defining embedded document attributes. Only
                applicable when ``ftype`` is
                :class:`fiftyone.core.fields.EmbeddedDocumentField`
            expand_schema (True): whether to add new fields to the schema
                (True) or simply validate that the field already exists with a
                consistent type (False)
            recursive (True): whether to recursively add embedded document
                fields
            validate (True): whether to validate the field against an existing
                field at the same path
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`

        Returns:
            True/False whether one or more fields or embedded fields were added
            to the document or its children

        Raises:
            ValueError: if a field in the schema is not compliant with an
                existing field of the same name
        """
        field = cls._create_field(
            path,
            ftype,
            embedded_doc_type=embedded_doc_type,
            subfield=subfield,
            fields=fields,
            **kwargs,
        )

        return cls.merge_field_schema(
            {path: field},
            expand_schema=expand_schema,
            recursive=recursive,
            validate=validate,
            dataset_doc=dataset_doc,
        )

    @classmethod
    def add_implied_field(
        cls,
        path,
        value,
        expand_schema=True,
        dynamic=False,
        recursive=True,
        validate=True,
        dataset_doc=None,
    ):
        """Adds the field or embedded field to the document, if necessary,
        inferring the field type from the provided value.

        Args:
            path: the field name or ``embedded.field.name``
            value: the field value
            expand_schema (True): whether to add new fields to the schema
                (True) or simply validate that the field already exists with a
                consistent type (False)
            dynamic (False): whether to declare dynamic embedded document
                fields
            recursive (True): whether to recursively add embedded document
                fields
            validate (True): whether to validate the field against an existing
                field at the same path
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`

        Returns:
            True/False whether one or more fields or embedded fields were added
            to the document or its children

        Raises:
            ValueError: if a field in the schema is not compliant with an
                existing field of the same name
        """
        field = cls._create_implied_field(path, value, dynamic)

        return cls.merge_field_schema(
            {path: field},
            expand_schema=expand_schema,
            recursive=recursive,
            validate=validate,
            dataset_doc=dataset_doc,
        )

    @classmethod
    def _create_field(
        cls,
        path,
        ftype,
        embedded_doc_type=None,
        subfield=None,
        fields=None,
        **kwargs,
    ):
        field_name = path.rsplit(".", 1)[-1]
        return create_field(
            field_name,
            ftype,
            embedded_doc_type=embedded_doc_type,
            subfield=subfield,
            fields=fields,
            **kwargs,
        )

    @classmethod
    def _create_implied_field(cls, path, value, dynamic):
        field_name = path.rsplit(".", 1)[-1]
        kwargs = get_implied_field_kwargs(value, dynamic=dynamic)
        return create_field(field_name, **kwargs)

    @classmethod
    def _get_default_fields(cls, dataset_doc=None):
        default_fields = set(
            get_default_fields(cls.__bases__[0], include_private=True)
        )

        if (
            dataset_doc is not None
            and dataset_doc.media_type == fom.GROUP
            and not cls._is_frames_doc
        ):
            default_fields.add(dataset_doc.group_field)

        return default_fields

    @classmethod
    def _rename_fields(cls, field_names, new_field_names, dataset_doc=None):
        """Renames the fields of the documents in this collection.

        Args:
            field_names: an iterable of field names
            new_field_names: an iterable of new field names
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`
        """
        if dataset_doc is None:
            dataset_doc = cls._dataset_doc()

        media_type = dataset_doc.media_type
        is_frame_field = cls._is_frames_doc

        default_fields = cls._get_default_fields()

        for field_name, new_field_name in zip(field_names, new_field_names):
            # pylint: disable=no-member
            existing_field = cls._fields.get(field_name, None)

            if field_name in default_fields:
                raise ValueError(
                    "Cannot rename default %s field '%s'"
                    % (cls._doc_name().lower(), field_name)
                )

            if existing_field is None:
                raise AttributeError(
                    "%s field '%s' does not exist"
                    % (cls._doc_name(), field_name)
                )

            # pylint: disable=no-member
            if new_field_name in cls._fields:
                raise ValueError(
                    "%s field '%s' already exists"
                    % (cls._doc_name(), new_field_name)
                )

            validate_field_name(
                new_field_name,
                media_type=media_type,
                is_frame_field=is_frame_field,
            )

            if fog.is_group_field(existing_field):
                dataset_doc.group_field = new_field_name

        cls._rename_fields_simple(field_names, new_field_names)

        for field_name, new_field_name in zip(field_names, new_field_names):
            cls._rename_field_schema(field_name, new_field_name, dataset_doc)

        dataset_doc.app_config._rename_paths(field_names, new_field_names)
        dataset_doc.save()

    @classmethod
    def _rename_embedded_fields(
        cls, field_names, new_field_names, sample_collection, dataset_doc=None
    ):
        """Renames the embedded field of the documents in this collection.

        Args:
            field_names: an iterable of "embedded.field.names"
            new_field_names: an iterable of "new.embedded.field.names"
            sample_collection: the
                :class:`fiftyone.core.samples.SampleCollection` being operated
                upon
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`
        """
        cls._rename_fields_collection(
            field_names, new_field_names, sample_collection
        )

        if isinstance(sample_collection, fod.Dataset):
            if dataset_doc is None:
                dataset_doc = cls._dataset_doc()

            dataset_doc.app_config._rename_paths(field_names, new_field_names)
            dataset_doc.save()

    @classmethod
    def _clone_fields(
        cls,
        field_names,
        new_field_names,
        sample_collection=None,
        dataset_doc=None,
    ):
        """Clones the field(s) of the documents in this collection.

        Args:
            field_names: an iterable of field names
            new_field_names: an iterable of new field names
            sample_collection (None): the
                :class:`fiftyone.core.samples.SampleCollection` being operated
                upon
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`
        """
        if dataset_doc is None:
            dataset_doc = cls._dataset_doc()

        media_type = dataset_doc.media_type
        is_frame_field = cls._is_frames_doc

        for field_name, new_field_name in zip(field_names, new_field_names):
            # pylint: disable=no-member
            existing_field = cls._fields.get(field_name, None)

            if existing_field is None:
                raise AttributeError(
                    "%s field '%s' does not exist"
                    % (cls._doc_name(), field_name)
                )

            # pylint: disable=no-member
            if new_field_name in cls._fields:
                raise ValueError(
                    "%s field '%s' already exists"
                    % (cls._doc_name(), new_field_name)
                )

            if fog.is_group_field(existing_field):
                raise ValueError(
                    "Cannot clone group field '%s'. Datasets may only have "
                    "one group field" % field_name
                )

            validate_field_name(
                new_field_name,
                media_type=media_type,
                is_frame_field=is_frame_field,
            )

        if sample_collection is None:
            cls._clone_fields_simple(field_names, new_field_names)
        else:
            cls._clone_fields_collection(
                field_names, new_field_names, sample_collection
            )

        for field_name, new_field_name in zip(field_names, new_field_names):
            cls._clone_field_schema(field_name, new_field_name, dataset_doc)

        dataset_doc.save()

    @classmethod
    def _clone_embedded_fields(
        cls, field_names, new_field_names, sample_collection
    ):
        """Clones the embedded field(s) of the documents in this collection.

        Args:
            field_names: an iterable of "embedded.field.names"
            new_field_names: an iterable of "new.embedded.field.names"
            sample_collection: the
                :class:`fiftyone.core.samples.SampleCollection` being operated
                upon
        """
        cls._clone_fields_collection(
            field_names, new_field_names, sample_collection
        )

    @classmethod
    def _clear_fields(cls, field_names, sample_collection=None):
        """Clears the field(s) of the documents in this collection.

        Args:
            field_names: an iterable of field names
            sample_collection (None): the
                :class:`fiftyone.core.samples.SampleCollection` being operated
                upon
        """
        for field_name in field_names:
            # pylint: disable=no-member
            if field_name not in cls._fields:
                raise AttributeError(
                    "%s field '%s' does not exist"
                    % (cls._doc_name(), field_name)
                )

        if sample_collection is None:
            cls._clear_fields_simple(field_names)
        else:
            cls._clear_fields_collection(field_names, sample_collection)

    @classmethod
    def _clear_embedded_fields(cls, field_names, sample_collection):
        """Clears the embedded field(s) on the documents in this collection.

        Args:
            field_names: an iterable of "embedded.field.names"
            sample_collection: the
                :class:`fiftyone.core.samples.SampleCollection` being operated
                upon
        """
        cls._clear_fields_collection(field_names, sample_collection)

    @classmethod
    def _delete_fields(cls, field_names, dataset_doc=None, error_level=0):
        """Deletes the field(s) from the documents in this collection.

        Args:
            field_names: an iterable of field names
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`
            error_level (0): the error level to use. Valid values are:

            -   0: raise error if a field cannot be deleted
            -   1: log warning if a field cannot be deleted
            -   2: ignore fields that cannot be deleted
        """
        if dataset_doc is None:
            dataset_doc = cls._dataset_doc()

        default_fields = cls._get_default_fields(dataset_doc=dataset_doc)

        del_fields = []
        for field_name in field_names:
            # pylint: disable=no-member
            if field_name in default_fields:
                fou.handle_error(
                    ValueError(
                        "Cannot delete default %s field '%s'"
                        % (cls._doc_name().lower(), field_name)
                    ),
                    error_level,
                )
            elif field_name not in cls._fields:
                fou.handle_error(
                    AttributeError(
                        "%s field '%s' does not exist"
                        % (cls._doc_name(), field_name)
                    ),
                    error_level,
                )
            else:
                del_fields.append(field_name)

        if not del_fields:
            return

        cls._delete_fields_simple(del_fields)

        for field_name in del_fields:
            cls._delete_field_schema(field_name, dataset_doc)

        dataset_doc.app_config._delete_paths(field_names)
        dataset_doc.save()

    @classmethod
    def _delete_embedded_fields(cls, field_names, dataset_doc=None):
        """Deletes the embedded field(s) from the documents in this collection.

        Args:
            field_names: an iterable of "embedded.field.names"
            dataset_doc (None): the
                :class:`fiftyone.core.odm.dataset.DatasetDocument`
        """
        if dataset_doc is None:
            dataset_doc = cls._dataset_doc()

        cls._delete_fields_simple(field_names)

        dataset_doc.app_config._delete_paths(field_names)
        dataset_doc.save()

    @classmethod
    def _rename_fields_simple(cls, field_names, new_field_names):
        if not field_names:
            return

        _field_names, _new_field_names = cls._handle_db_fields(
            field_names, new_field_names
        )

        rename_expr = {k: v for k, v in zip(_field_names, _new_field_names)}

        collection_name = cls.__name__
        collection = get_db_conn()[collection_name]
        collection.update_many({}, {"$rename": rename_expr})

    @classmethod
    def _rename_fields_collection(
        cls, field_names, new_field_names, sample_collection
    ):
        from fiftyone import ViewField as F

        if not field_names:
            return

        _field_names, _new_field_names = cls._handle_db_fields(
            field_names, new_field_names
        )

        if cls._is_frames_doc:
            prefix = sample_collection._FRAMES_PREFIX
            field_names = [prefix + f for f in field_names]
            new_field_names = [prefix + f for f in new_field_names]
            _field_names = [prefix + f for f in _field_names]
            _new_field_names = [prefix + f for f in _new_field_names]

        view = sample_collection.view()
        for field_name, new_field_name in zip(_field_names, _new_field_names):
            new_base = new_field_name.rsplit(".", 1)[0]
            if "." in field_name:
                base, leaf = field_name.rsplit(".", 1)
            else:
                base, leaf = field_name, ""

            if new_base == base:
                expr = F(leaf)
            else:
                expr = F("$" + field_name)

            view = view.set_field(new_field_name, expr, _allow_missing=True)

        view = view.mongo([{"$unset": _field_names}])

        #
        # Ideally only the embedded field would be saved, but the `$merge`
        # operator will always overwrite top-level fields of each document, so
        # we limit the damage by projecting onto the modified fields
        #
        field_roots = sample_collection._get_root_fields(
            field_names + new_field_names
        )
        view.save(field_roots)

    @classmethod
    def _clone_fields_simple(cls, field_names, new_field_names):
        if not field_names:
            return

        _field_names, _new_field_names = cls._handle_db_fields(
            field_names, new_field_names
        )

        set_expr = {v: "$" + k for k, v in zip(_field_names, _new_field_names)}

        collection_name = cls.__name__
        collection = get_db_conn()[collection_name]
        collection.update_many({}, [{"$set": set_expr}])

    @classmethod
    def _clone_fields_collection(
        cls, field_names, new_field_names, sample_collection
    ):
        from fiftyone import ViewField as F

        if not field_names:
            return

        _field_names, _new_field_names = cls._handle_db_fields(
            field_names, new_field_names
        )

        if cls._is_frames_doc:
            prefix = sample_collection._FRAMES_PREFIX
            field_names = [prefix + f for f in field_names]
            new_field_names = [prefix + f for f in new_field_names]
            _field_names = [prefix + f for f in _field_names]
            _new_field_names = [prefix + f for f in _new_field_names]

        view = sample_collection.view()
        for field_name, new_field_name in zip(_field_names, _new_field_names):
            new_base = new_field_name.rsplit(".", 1)[0]
            if "." in field_name:
                base, leaf = field_name.rsplit(".", 1)
            else:
                base, leaf = field_name, ""

            if new_base == base:
                expr = F(leaf)
            else:
                expr = F("$" + field_name)

            view = view.set_field(new_field_name, expr, _allow_missing=True)

        #
        # Ideally only the embedded field would be merged in, but the `$merge`
        # operator will always overwrite top-level fields of each document, so
        # we limit the damage by projecting onto the modified fields
        #
        field_roots = sample_collection._get_root_fields(new_field_names)
        view.save(field_roots)

    @classmethod
    def _clear_fields_simple(cls, field_names):
        if not field_names:
            return

        _field_names = cls._handle_db_fields(field_names)

        collection_name = cls.__name__
        collection = get_db_conn()[collection_name]
        collection.update_many({}, {"$set": {k: None for k in _field_names}})

    @classmethod
    def _clear_fields_collection(cls, field_names, sample_collection):
        if not field_names:
            return

        _field_names = cls._handle_db_fields(field_names)

        if cls._is_frames_doc:
            prefix = sample_collection._FRAMES_PREFIX
            field_names = [prefix + f for f in field_names]
            _field_names = [prefix + f for f in _field_names]

        view = sample_collection.view()
        for field_name in _field_names:
            view = view.set_field(field_name, None, _allow_missing=True)

        #
        # Ideally only the embedded field would be merged in, but the `$merge`
        # operator will always overwrite top-level fields of each document, so
        # we limit the damage by projecting onto the modified fields
        #
        field_roots = sample_collection._get_root_fields(field_names)
        view.save(field_roots)

    @classmethod
    def _delete_fields_simple(cls, field_names):
        if not field_names:
            return

        _field_names = cls._handle_db_fields(field_names)

        collection_name = cls.__name__
        collection = get_db_conn()[collection_name]
        collection.update_many({}, [{"$unset": _field_names}])

    @classmethod
    def _handle_db_field(cls, field_name, new_field_name=None):
        # pylint: disable=no-member
        field = cls._fields.get(field_name, None)

        if field is None or field.db_field is None:
            if new_field_name is not None:
                return field_name, new_field_name

            return field_name

        _field_name = field.db_field

        if new_field_name is not None:
            _new_field_name = _get_db_field(field, new_field_name)
            return _field_name, _new_field_name

        return _field_name

    @classmethod
    def _handle_db_fields(cls, field_names, new_field_names=None):
        if new_field_names is not None:
            return zip(
                *[
                    cls._handle_db_field(f, new_field_name=n)
                    for f, n in zip(field_names, new_field_names)
                ]
            )

        return tuple(cls._handle_db_field(f) for f in field_names)

    @classmethod
    def _merge_field(
        cls, path, field, dataset_doc, validate=True, recursive=True
    ):
        chunks = path.split(".")
        field_name = chunks[-1]

        # Handle embedded fields
        root = None
        doc = cls
        for chunk in chunks[:-1]:
            if root is None:
                root = chunk
            else:
                root += "." + chunk

            schema = doc._fields

            if chunk not in schema:
                raise ValueError(
                    "Cannot infer an appropriate type for non-existent %s "
                    "field '%s' while defining embedded field '%s'"
                    % (cls._doc_name(), root, path)
                )

            doc = schema[chunk]

            if isinstance(doc, fof.ListField):
                doc = doc.field

            if not isinstance(doc, fof.EmbeddedDocumentField):
                raise ValueError(
                    "Cannot define schema for embedded %s field '%s' because "
                    "field '%s' is a %s, not an %s"
                    % (
                        cls._doc_name(),
                        path,
                        root,
                        type(doc),
                        fof.EmbeddedDocumentField,
                    )
                )

        if isinstance(field, fof.ObjectIdField) and field_name.startswith("_"):
            field_name = field_name[1:]

        if field_name == "id":
            return

        if field_name in doc._fields:
            existing_field = doc._fields[field_name]

            if recursive and isinstance(
                existing_field, fof.EmbeddedDocumentField
            ):
                return existing_field._merge_fields(
                    path, field, validate=validate, recursive=recursive
                )

            if validate:
                validate_fields_match(path, field, existing_field)

            return

        media_type = dataset_doc.media_type
        is_frame_field = cls._is_frames_doc

        validate_field_name(
            field_name,
            media_type=media_type,
            is_frame_field=is_frame_field,
        )

        if fog.is_group_field(field):
            if is_frame_field:
                raise ValueError(
                    "Cannot create frame-level group field '%s'. "
                    "Group fields must be top-level sample fields" % field_name
                )

            # `group_field` could be None here if we're in the process
            # of merging one dataset's schema into another
            if dataset_doc.group_field not in (None, field_name):
                raise ValueError(
                    "Cannot add group field '%s'. Datasets may only "
                    "have one group field" % field_name
                )

        return {path: field}

    @classmethod
    def _add_field_schema(cls, path, field, dataset_doc):
        chunks = path.split(".")
        name = chunks[-1]

        field_docs = dataset_doc[cls._fields_attr()]

        # Handle embedded fields
        root = None
        doc = cls
        for chunk in chunks[:-1]:
            if root is None:
                root = chunk
            else:
                root += "." + chunk

            found = False
            for field_doc in field_docs:
                if field_doc.name == chunk:
                    field_docs = field_doc.fields
                    found = True
                    break

            if not found:
                raise ValueError(
                    "Cannot add embedded %s field '%s' because field '%s' has "
                    "not been defined" % (cls._doc_name(), path, root)
                )

            doc = doc._fields[chunk]

            if isinstance(doc, fof.ListField):
                doc = doc.field

            if not isinstance(doc, fof.EmbeddedDocumentField):
                raise ValueError(
                    "Cannot define schema for embedded %s field '%s' because "
                    "field '%s' is a %s, not an %s"
                    % (
                        cls._doc_name(),
                        path,
                        root,
                        type(doc),
                        fof.EmbeddedDocumentField,
                    )
                )

        field = field.copy()

        # Allow for the possibility that name != field.name
        field.db_field = _get_db_field(field, name)
        field.name = name

        doc._declare_field(field)
        field_docs.append(SampleFieldDocument.from_field(field))

    @classmethod
    def _declare_field(cls, field_or_doc):
        if isinstance(field_or_doc, SampleFieldDocument):
            field = field_or_doc.to_field()
        else:
            field = field_or_doc

        prev = cls._fields.pop(field.name, None)
        cls._fields[field.name] = field

        if prev is None:
            cls._fields_ordered += (field.name,)
        else:
            field.required = prev.required
            field.null = prev.null

        setattr(cls, field.name, field)

    @classmethod
    def _rename_field_schema(cls, field_name, new_field_name, dataset_doc):
        # pylint: disable=no-member
        field = cls._fields.pop(field_name)
        new_db_field = _get_db_field(field, new_field_name)

        field.name = new_field_name
        field.db_field = new_db_field

        cls._fields[new_field_name] = field
        cls._fields_ordered = tuple(
            (fn if fn != field_name else new_field_name)
            for fn in cls._fields_ordered
        )
        delattr(cls, field_name)

        try:
            if issubclass(cls, Document):
                setattr(cls, new_field_name, field)
        except TypeError:
            pass

        fields = getattr(dataset_doc, cls._fields_attr())

        for f in fields:
            if f.name == field_name:
                f.name = new_field_name
                f.db_field = new_db_field

    @classmethod
    def _clone_field_schema(cls, field_name, new_field_name, dataset_doc):
        # pylint: disable=no-member
        field = cls._fields[field_name]
        cls._add_field_schema(new_field_name, field, dataset_doc)

    @classmethod
    def _delete_field_schema(cls, field_name, dataset_doc):
        # pylint: disable=no-member
        del cls._fields[field_name]
        cls._fields_ordered = tuple(
            fn for fn in cls._fields_ordered if fn != field_name
        )
        delattr(cls, field_name)

        fields = getattr(dataset_doc, cls._fields_attr())

        # This is intentionally implemented without creating a new list, since
        # clips datasets directly use their source dataset's frame fields
        for idx, f in enumerate(fields):
            if f.name == field_name:
                del fields[idx]
                break

    def _update(self, object_id, update_doc, filtered_fields=None, **kwargs):
        """Updates an existing document.

        Helper method; should only be used inside
        :meth:`DatasetSampleDocument.save`.
        """
        updated_existing = True

        collection = self._get_collection()

        select_dict = {"_id": object_id}

        extra_updates = self._extract_extra_updates(
            update_doc, filtered_fields
        )

        if update_doc:
            result = collection.update_one(
                select_dict, update_doc, upsert=True
            ).raw_result
            if result is not None:
                updated_existing = result.get("updatedExisting")

        for update, element_id in extra_updates:
            result = collection.update_one(
                select_dict,
                update,
                array_filters=[{"element._id": element_id}],
                upsert=True,
            ).raw_result

            if result is not None:
                updated_existing = updated_existing and result.get(
                    "updatedExisting"
                )

        return updated_existing

    def _extract_extra_updates(self, update_doc, filtered_fields):
        """Extracts updates for filtered list fields that need to be updated
        by ID, not relative position (index).
        """
        extra_updates = []

        #
        # Check for illegal modifications
        # Match the list, or an indexed item in the list, but not a field
        # of an indexed item of the list:
        #   my_detections.detections          <- MATCH
        #   my_detections.detections.1        <- MATCH
        #   my_detections.detections.1.label  <- NO MATCH
        #
        if filtered_fields:
            for d in update_doc.values():
                for k in d.keys():
                    for ff in filtered_fields:
                        if k.startswith(ff) and not k[len(ff) :].lstrip(
                            "."
                        ).count("."):
                            raise ValueError(
                                "Modifying root of filtered list field '%s' "
                                "is not allowed" % k
                            )

        if filtered_fields and "$set" in update_doc:
            d = update_doc["$set"]
            del_keys = []

            for k, v in d.items():
                filtered_field = None
                for ff in filtered_fields:
                    if k.startswith(ff):
                        filtered_field = ff
                        break

                if filtered_field:
                    element_id, el_filter = self._parse_id_and_array_filter(
                        k, filtered_field
                    )
                    extra_updates.append(
                        ({"$set": {el_filter: v}}, element_id)
                    )

                    del_keys.append(k)

            for k in del_keys:
                del d[k]

            if not update_doc["$set"]:
                del update_doc["$set"]

        return extra_updates

    def _parse_id_and_array_filter(self, list_element_field, filtered_field):
        """Converts the ``list_element_field`` and ``filtered_field`` to an
        element object ID and array filter.

        Example::

            Input:
                list_element_field = "test_dets.detections.1.label"
                filtered_field = "test_dets.detections"

            Output:
                ObjectId("5f2062bf27c024654f5286a0")
                "test_dets.detections.$[element].label"
        """
        el = self
        for field_name in filtered_field.split("."):
            el = el[field_name]

        el_fields = (
            list_element_field[len(filtered_field) :].lstrip(".").split(".")
        )
        idx = int(el_fields.pop(0))

        el = el[idx]
        el_filter = ".".join([filtered_field, "$[element]"] + el_fields)

        return el._id, el_filter

    @classmethod
    def _get_fields_ordered(cls, include_private=False, use_db_fields=False):
        field_names = cls._fields_ordered

        if not include_private:
            field_names = tuple(
                f for f in field_names if not f.startswith("_")
            )

        if use_db_fields:
            field_names = cls._to_db_fields(field_names)

        return field_names

    @classmethod
    def _to_db_fields(cls, field_names):
        return tuple(cls._fields[f].db_field or f for f in field_names)


class NoDatasetMixin(object):
    """Mixin for :class:`fiftyone.core.odm.document.SerializableDocument`
    subtypes that are not backed by a dataset.
    """

    # Subtypes must declare this
    _is_frames_doc = None

    def __getattr__(self, name):
        return self.get_field(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self.set_field(name, value)

    def _get_field_names(self, include_private=False, use_db_fields=False):
        field_names = tuple(self._data.keys())

        if not include_private:
            field_names = tuple(
                f for f in field_names if not f.startswith("_")
            )

        if use_db_fields:
            field_names = self._to_db_fields(field_names)

        return field_names

    def _to_db_fields(self, field_names):
        db_fields = []

        for field_name in field_names:
            if field_name == "id":
                db_fields.append("_id")
            elif isinstance(
                self._data.get(field_name, None), ObjectId
            ) and not field_name.startswith("_"):
                db_fields.append("_" + field_name)
            else:
                db_fields.append(field_name)

        return tuple(db_fields)

    def _get_repr_fields(self):
        return self.field_names

    @classmethod
    def _doc_name(cls):
        return "Frame" if cls._is_frames_doc else "Sample"

    @property
    def field_names(self):
        return self._get_field_names(include_private=False)

    @property
    def collection_name(self):
        return None

    @property
    def in_db(self):
        return False

    @staticmethod
    def _get_default(field):
        if field.null:
            return None

        if field.default is not None:
            value = field.default

            if callable(value):
                value = value()

            if isinstance(value, list) and value.__class__ != list:
                value = list(value)
            elif isinstance(value, tuple) and value.__class__ != tuple:
                value = tuple(value)
            elif isinstance(value, dict) and value.__class__ != dict:
                value = dict(value)

            return value

        raise ValueError("Field '%s' has no default" % field)

    def has_field(self, field_name):
        try:
            return field_name in self._data
        except AttributeError:
            # If `_data` is not initialized
            return False

    def get_field(self, field_name):
        try:
            return self._data[field_name]
        except KeyError:
            raise AttributeError(
                "%s has no field '%s'" % (self._doc_name(), field_name)
            )

    def set_field(
        self,
        field_name,
        value,
        create=True,
        validate=True,
        dynamic=False,
    ):
        if not create and not self.has_field(field_name):
            raise ValueError(
                "%s has no field '%s'" % (self._doc_name(), field_name)
            )

        validate_field_name(field_name)
        self._data[field_name] = value

    def clear_field(self, field_name):
        if field_name in self.default_fields:
            default_value = self._get_default(self.default_fields[field_name])
            self.set_field(field_name, default_value, create=False)
            return

        if not self.has_field(field_name):
            raise ValueError(
                "%s has no field '%s'" % (self._doc_name(), field_name)
            )

        self._data.pop(field_name)

    def to_dict(self, extended=False):
        d = {}
        for k, v in self._data.items():
            # Store ObjectIds in private fields in the DB
            if k == "id":
                k = "_id"
            elif isinstance(v, ObjectId) and not k.startswith("_"):
                k = "_" + k

            d[k] = serialize_value(v, extended=extended)

        return d

    @classmethod
    def from_dict(cls, d, extended=False):
        kwargs = {}
        for k, v in d.items():
            v = deserialize_value(v)

            if k == "_id":
                k = "id"
            elif isinstance(v, ObjectId) and k.startswith("_"):
                k = k[1:]

            kwargs[k] = v

        return cls(**kwargs)

    def save(self):
        pass

    def _save(self, deferred=False):
        pass

    def reload(self):
        pass

    def delete(self):
        pass


def _get_db_field(field, new_field_name):
    if field.db_field is None:
        return None

    # This is hacky, but we must account for the fact that ObjectIdField often
    # uses db_field = "_<field_name>"
    if field.db_field == "_" + field.name:
        return "_" + new_field_name

    return new_field_name
