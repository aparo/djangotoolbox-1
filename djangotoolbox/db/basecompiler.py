import datetime
import random

from django.conf import settings
from django.db.models.fields import NOT_PROVIDED, DecimalField
from django.db.models.sql import aggregates as sqlaggregates
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.constants import LOOKUP_SEP, MULTI, SINGLE
from django.db.models.sql.where import AND, OR
from django.db.utils import DatabaseError, IntegrityError
from django.utils.tree import Node


EMULATED_OPS = {
    'exact': lambda x, y: y in x if isinstance(x, (list,tuple)) else x == y,
    'iexact': lambda x, y: x.lower() == y.lower(),
    'startswith': lambda x, y: x.startswith(y),
    'istartswith': lambda x, y: x.lower().startswith(y.lower()),
    'isnull': lambda x, y: x is None if y else x is not None,
    'in': lambda x, y: x in y,
    'lt': lambda x, y: x < y,
    'lte': lambda x, y: x <= y,
    'gt': lambda x, y: x > y,
    'gte': lambda x, y: x >= y,
}


class NonrelQuery(object):
    """
    Base class for nonrel queries.

    Compilers build a nonrel query when they want to fetch some data.
    They work by first allowing sql.compiler.SQLCompiler to partly build
    a sql.Query, constructing a NonrelQuery query on top of it, and then
    iterating over its results.

    This class provides in-memory filtering and ordering and a
    framework for converting SQL constraint tree built by Django to a
    representation more suitable for most NoSQL databases.

    TODO: Replace with FetchCompiler, there are to many query concepts
          around, and it isn't a good abstraction for NoSQL databases.

    TODO: Nonrel currently uses constraint's tree built by Django to
          handle filtering. However, Django intermingles translating
          its lookup syntax abstraction to a logical formula with some
          preprocessing for joins, and this results in multiple hacks
          in nonrel. It would be a nice (though likely sizable) project
          to build some abstraction that would be suitable for both
          contexts.
    """

    # ----------------------------------------------
    # Public API
    # ----------------------------------------------
    def __init__(self, compiler, fields):
        self.compiler = compiler
        self.connection = compiler.connection
        self.query = self.compiler.query # sql.Query
        self.fields = fields
        self._negated = False

    def fetch(self, low_mark=0, high_mark=None):
        raise NotImplementedError('Not implemented')

    def count(self, limit=None):
        raise NotImplementedError('Not implemented')

    def delete(self):
        """
        Called by NonrelDeleteCompiler after it builds a delete query.
        """
        raise NotImplementedError('Not implemented')

    def order_by(self, ordering):
        raise NotImplementedError('Not implemented')

    def add_filter(self, column, lookup_type, negated, value, field):
        """
        Adds a single constraint to the query. Called by add_filters for
        each constraint leaf in the WHERE tree built by Django.

        :param column: Database property name
        :param lookup_type: Django's lookup name (e.g. "startswith")
        :param negated: Is the lookup negated
        :param value: Lookup argument, e.g. value to compare with
        :param field: Field the value comes from; only use it to learn
                      properties of value

        TODO: Rename, this methods deals with constraints rather than
              what Django calls lookups.
        """
        raise NotImplementedError('Not implemented')

    def add_filters(self, filters):
        """
        Converts a constraint tree (sql.where.WhereNode) created by
        Django's SQL query machinery to nonrel style filters, calling
        add_filter for each constraint.

        This assumes the database doesn't support alternatives of
        constraints, you should override this method if it does.

        TODO: Simulate both conjunctions and alternatives in general
              let GAE override conjunctions not to split them into
              multiple queries.
        """
        if filters.negated:
            self._negated = not self._negated

        if not self._negated and filters.connector != AND:
            raise DatabaseError('Only AND filters are supported.')

        # Remove unneeded children from the tree.
        children = self._get_children(filters.children)

        if self._negated and filters.connector != OR and len(children) > 1:
            raise DatabaseError("When negating a whole filter subgroup "
                                "(e.g., a Q object) the subgroup filters must "
                                "be connected via OR, so the non-relational "
                                "backend can convert them like this: "
                                '"not (a OR b) => (not a) AND (not b)".')

        # Recursively call the method for internal tree nodes, add a
        # filter for each leaf.
        for child in children:
            if isinstance(child, Node):
                self.add_filters(child)
                continue
            column, lookup_type, value, field = self._decode_child(child)
            self.add_filter(column, lookup_type, self._negated, value, field)

        if filters.negated:
            self._negated = not self._negated

    # ----------------------------------------------
    # Internal API for reuse by subclasses
    # ----------------------------------------------
    def _decode_child(self, child):
        """
        Produces arguments suitable for add_filter from a WHERE tree
        leaf (a tuple).
        """

        # TODO: Call get_db_prep_lookup directly, constrain.process
        # doesn't do much more.
        constraint, lookup_type, annotation, value = child
        packed, value = constraint.process(lookup_type, value, self.connection)
        alias, column, db_type = packed
        field = constraint.field

        opts = self.query.model._meta
        if alias and alias != opts.db_table:
            raise DatabaseError("This database doesn't support JOINs "
                                "and multi-table inheritance.")

        # For parent.child_set queries the field held by the constraint
        # is the parent's primary key, while the field the filter
        # should consider is the child's foreign key field.
        if column != field.column:
            assert field.primary_key
            field = (f for f in opts.fields if f.column == column).next()
            assert field.rel is not None

        value = self._normalize_lookup_value(
            lookup_type, value, field, annotation)

        return column, lookup_type, value, field

    def _normalize_lookup_value(self, lookup_type, value, field, annotation):
        """
        Undoes preparations done by Field.get_db_prep_lookup not
        suitable for nonrel back-ends and calls value_for_db_* for
        standard fields that don't do it on their own for lookups.

        TODO: Blank Field.get_db_prep_lookup instead?
        TODO: Move to DatabaseOperations too (the code this counters
              or calls is there)?
        """

        # Undo Field.get_db_prep_lookup putting most values in a list.
        if lookup_type not in ('in', 'range', 'year') and \
            isinstance(value, (tuple, list)):

            if len(value) > 1:
                raise DatabaseError('Filter lookup type was: %s. Expected the '
                                'filters value not to be a list. Only "in"-filters '
                                'can be used with lists.'
                                % lookup_type)
            elif lookup_type == 'isnull':
                value = annotation
            else:
                value = value[0]

        # Remove percents added by Field.get_db_prep_lookup (useful
        # if one were to use the value in a LIKE expression).
        if lookup_type in ('startswith', 'istartswith'):
            value = value[:-1]
        elif lookup_type in ('endswith', 'iendswith'):
            value = value[1:]
        elif lookup_type in ('contains', 'icontains'):
            value = value[1:-1]

        # Workaround for Django only defining get_db_prep_save, rather
        # than get_db_prep_value, causing DatabaseOperations.value_to_db_decimal
        # not to be applied for lookups.
        # TODO: Should be removed if it changes.
        if isinstance(field, DecimalField):
            value = self.connection.ops.value_to_db_decimal(
                field.to_python(value), field.max_digits, field.decimal_places)

        return value

    def _get_children(self, children):
        """
        Filters out nodes of the given contraint tree not needed for
        nonrel queries.
        """
        result = []
        for child in children:

            # Remove leafs that were automatically added by
            # sql.Query.add_filter to handle negations of outer joins.
            if isinstance(child, tuple):
                constraint = child[0]
                lookup_type = child[1]
                if lookup_type == 'isnull' and constraint.field is None:
                    continue
            result.append(child)
        return result

    def _matches_filters(self, entity, filters):
        """
        Checks if an entity returned by the database would match
        constraints in a WHERE tree.

        TODO: Use _decode_child and _normalize_lookup_value.
        """
        # Filters without rules match everything
        if not filters.children:
            return True

        result = filters.connector == AND

        for child in filters.children:
            if isinstance(child, Node):
                submatch = self._matches_filters(entity, child)
            else:
                constraint, lookup_type, annotation, value = child
                packed, value = constraint.process(lookup_type, value, self.connection)
                alias, column, db_type = packed
                if alias != self.query.model._meta.db_table:
                    raise DatabaseError("This database doesn't support JOINs "
                                        "and multi-table inheritance.")

                # Django fields always return a list (see Field.get_db_prep_lookup)
                # except if get_db_prep_lookup got overridden by a subclass
                if lookup_type != 'in' and isinstance(value, (tuple, list)):
                    if len(value) > 1:
                        raise DatabaseError('Filter lookup type was: %s. '
                            'Expected the filters value not to be a list. '
                            'Only "in"-filters can be used with lists.'
                            % lookup_type)
                    elif lookup_type == 'isnull':
                        value = annotation
                    else:
                        value = value[0]

                if entity[column] is None:
                    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
                        submatch = lookup_type in ('lt', 'lte')
                    elif lookup_type in ('startswith', 'contains', 'endswith', 'iexact',
                                         'istartswith', 'icontains', 'iendswith'):
                        submatch = False
                    else:
                        submatch = EMULATED_OPS[lookup_type](entity[column], value)
                else:
                    submatch = EMULATED_OPS[lookup_type](entity[column], value)

            if filters.connector == OR and submatch:
                result = True
                break
            elif filters.connector == AND and not submatch:
                result = False
                break

        if filters.negated:
            return not result
        return result

    def _order_in_memory(self, lhs, rhs):
        for column, descending in self.compiler._get_ordering():
            result = cmp(lhs.get(column), rhs.get(column))
            if descending:
                result *= -1
            if result != 0:
                return result
        return 0


class NonrelCompiler(SQLCompiler):
    """
    Base class for data fetching back-end compilers.

    Note that nonrel compilers derive from sql.compiler.SQLCompiler and
    thus hold a reference to a sql.Query, not a NonrelQuery.

    TODO: Separate FetchCompiler from the abstract NonrelCompiler.
    """

    def __init__(self, query, connection, using):
        """
        Save pointers to DatabaseCreation and DatabaseOperations for
        quick access in conversion wrappers.
        """
        super(NonrelCompiler, self).__init__(query, connection, using)
        self.creation = self.connection.creation
        self.ops = self.connection.ops

    # ----------------------------------------------
    # Public API
    # ----------------------------------------------
    def results_iter(self):
        """
        Returns an iterator over the results from executing this query.
        """
        self.check_query()
        fields = self.get_fields()
        low_mark = self.query.low_mark
        high_mark = self.query.high_mark
        for entity in self.build_query(fields).fetch(low_mark, high_mark):
            yield self._make_result(entity, fields)

    def has_results(self):
        return self.get_count(check_exists=True)

    def execute_sql(self, result_type=MULTI):
        """
        Handles SQL-like aggregate queries. This class only emulates COUNT
        by using abstract NonrelQuery.count method.
        """
        aggregates = self.query.aggregate_select.values()
        if aggregates:
            assert len(aggregates) == 1
            aggregate = aggregates[0]
            assert isinstance(aggregate, sqlaggregates.Count)
            opts = self.query.get_meta()
            assert aggregate.col == '*' or aggregate.col == (opts.db_table, opts.pk.column)
            count = self.get_count()
            if result_type is SINGLE:
                return [count]
            elif result_type is MULTI:
                return [[count]]
        raise NotImplementedError('The database backend only supports '
                                  'count() queries.')

    # ----------------------------------------------
    # Additional NonrelCompiler API
    # ----------------------------------------------
    def _make_result(self, entity, fields):
        """
        Decodes values for the given fields from the database entity.

        The entity is assumed to be a Mapping using field database column
        names as keys. Decodes values using convert_value_from_db as
        well as the standard convert_values.
        """
        result = []
        for field in fields:
            value = entity.get(field.column, NOT_PROVIDED)
            if value is NOT_PROVIDED:
                value = field.get_default()
            else:
                value = self.convert_value_from_db(value, field)
                value = self.query.convert_values(value, field, self.connection)
            if value is None and not field.null:
                raise IntegrityError("Non-nullable field %s can't be None!"
                                     % field.name)
            result.append(value)
        return result

    def check_query(self):
        """
        Checks if the current query is supported by the database.

        TODO: Short description of what is expected not to be available.
        """
        if (len([a for a in self.query.alias_map if self.query.alias_refcount[a]]) > 1
                or self.query.distinct or self.query.extra or self.query.having):
            raise DatabaseError('This query is not supported by the database.')

    def get_count(self, check_exists=False):
        """
        Counts objects matching the current filters / constraints.

        :param check_exists: Only check if any object matches
        """
        if check_exists:
            high_mark = 1
        else:
            high_mark = self.query.high_mark
        return self.build_query().count(high_mark)

    def build_query(self, fields=None):
        """Prepares a NonrelQuery to be executed on the database."""
        if fields is None:
            fields = self.get_fields()
        query = self.query_class(self, fields)
        query.add_filters(self.query.where)
        query.order_by(self._get_ordering())

        # This at least satisfies the most basic unit tests.
        if settings.DEBUG:
            self.connection.queries.append({'sql': repr(query)})
        return query

    def get_fields(self):
        """
        Returns fields which should get loaded from the back-end by the
        current query.
        """

        # We only set this up here because related_select_fields isn't
        # populated until execute_sql() has been called.
        if self.query.select_fields:
            fields = self.query.select_fields + self.query.related_select_fields
        else:
            fields = self.query.model._meta.fields

        # If the field was deferred, exclude it from being passed
        # into `resolve_columns` because it wasn't selected.
        only_load = self.deferred_to_columns()
        if only_load:
            db_table = self.query.model._meta.db_table
            only_load = dict((k, v) for k, v in only_load.items()
                             if v or k == db_table)
            if len(only_load.keys()) > 1:
                raise DatabaseError('Multi-table inheritance is not supported '
                                    'by non-relational DBs.' + repr(only_load))
            fields = [f for f in fields if db_table in only_load and
                      f.column in only_load[db_table]]

        query_model = self.query.model
        if query_model._meta.proxy:
            query_model = query_model._meta.proxy_for_model

        for field in fields:
            if field.model._meta != query_model._meta:
                raise DatabaseError('Multi-table inheritance is not supported '
                                    'by non-relational DBs.')
        return fields

    def _get_ordering(self):
        opts = self.query.get_meta()
        if not self.query.default_ordering:
            ordering = self.query.order_by
        else:
            ordering = self.query.order_by or opts.ordering
        for order in ordering:
            if LOOKUP_SEP in order:
                raise DatabaseError("Ordering can't span tables on non-relational backends (%s)" % order)
            if order == '?':
                raise DatabaseError("Randomized ordering isn't supported by the backend")
            order = order.lstrip('+')
            descending = order.startswith('-')
            field = order.lstrip('-')
            if field == 'pk':
                field = opts.pk.name
            if not self.query.standard_ordering:
                descending = not descending
            yield (opts.get_field(field).column, descending)

    def convert_value_for_db(self, value, field, lookup=False):
        """
        Does type-conversions needed before storing a value in the
        the database or using it as a filter parameter.

        This is a convience wrapper that only precomputes field_kind
        and nonrel_db_type and calls DatabaseOperations method to do
        the real work; you should typically extend the operations class
        method, but only call this one.

        Note that compilers may do conversions without building a
        NonrelQuery, thus we need to define this method here rather
        than on the query class.

        :param value: A value to be passed to the database driver
        :param field: A field the value comes from
        :param lookup: Is the value being prepared as a filter
                       parameter or for storage
        """
        return self.ops.convert_value_for_db(value, field,
            field.get_internal_type(),
            self.creation.nonrel_db_type(field), lookup)

    def convert_value_from_db(self, value, field):
        """
        Performs deconversions defined by back-end's DatabaseOperations.

        :param value: A value received from the database client
        :param field: A field the value is meant for
        """
        return self.ops.convert_value_from_db(value, field,
            field.get_internal_type(),
            self.creation.nonrel_db_type(field))


class NonrelInsertCompiler(NonrelCompiler):
    """
    Base class for all compliers that create new entities or objects
    in the database. It has to define execute_sql method due to being
    used in place of a SQLInsertCompiler.
    """
    def execute_sql(self, return_id=False):
        data = {}
        for (field, value), column in zip(self.query.values, self.query.columns):
            if field is not None:
                if not field.null and value is None:
                    raise IntegrityError("You can't set %s (a non-nullable "
                                         "field) to None!" % field.name)
                value = self.convert_value_for_db(value, field)
            data[column] = value
        return self.insert(data, return_id=return_id)

    def insert(self, values, return_id):
        """
        :param values: The model object as a list of (column, value) pairs
        :param return_id: Whether to return the id of the newly created entity
        """
        raise NotImplementedError


class NonrelUpdateCompiler(NonrelCompiler):
    def execute_sql(self, result_type):
        values = []
        for field, _, value in self.query.values:
            if hasattr(value, 'prepare_database_save'):
                value = value.prepare_database_save(field)
            else:
                value = field.get_db_prep_save(value, connection=self.connection)
            value = self.convert_value_for_db(value, field)
            values.append((field, value))
        return self.update(values)

    def update(self, values):
        """
        :param values: A list of (field, new-value) pairs
        """
        raise NotImplementedError


class NonrelDeleteCompiler(NonrelCompiler):
    def execute_sql(self, result_type=MULTI):
        self.build_query([self.query.get_meta().pk]).delete()
