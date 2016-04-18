from django.db import models
from django.db.models.query import F
from six import string_types

from .constants import collection_kinds


class HierarchyRelationsFilter(object):
    """
    Helper class for efficiently making queries based on relations between models in the Collection hierarchy via Roles/Memberships.

    To use, instantiate an instance of `HierarchyRelationsFilter`, passing in a queryset. Then, to perform hierarchy-based queries
    on the queryset, call the `filter_by_hierarchy` method on the `HierarchyRelationsFilter` instance, passing arguments fixing values
    for models in the hierarchy structure, or linking them to fields on the base model being filtered (via F expressions).
    """

    _role_extra = {
        "tables": [
            '"{facilityuser_table}" AS "source_user"',
            '"{role_table}" AS "role"',
        ],
        "where": [
            "role.user_id = source_user.id",
            "role.collection_id = ancestor_collection.id",
        ]
    }

    _collection_extra = {
        "tables": [
            '"{collection_table}" AS "ancestor_collection"',
            '"{collection_table}" AS "descendant_collection"',
        ],
        "where": [
            "descendant_collection.lft BETWEEN ancestor_collection.lft AND ancestor_collection.rght",
        ]
    }

    _membership_extra = {
        "tables": [
            '"{facilityuser_table}" AS "target_user"',
            '"{membership_table}" AS "membership"',
        ],
        "where": [
            "membership.user_id = target_user.id",
            "membership.collection_id = descendant_collection.id",
        ]
    }

    def __init__(self, queryset):

        # convert the provided argument from a Model class into a QuerySet as needed
        if issubclass(queryset, models.Model):
            queryset = queryset.objects.all()
        self.queryset = queryset

        # import auth models here to avoid circular imports
        from .models import Role, Collection, Membership, FacilityUser

        # retrieve the table names that will be used as context for building queries
        self._table_names = {
            "role_table": Role._meta.db_table,
            "collection_table": Collection._meta.db_table,
            "membership_table": Membership._meta.db_table,
            "facilityuser_table": FacilityUser._meta.db_table,
        }

    def _add_extras(self, queryset, **kwargs):
        extras = {}
        if "tables" in kwargs:
            extras["tables"] = [table.format(**self._table_names) for table in kwargs["tables"]]
        if "where" in kwargs:
            extras["where"] = kwargs["where"]
        return queryset.extra(**extras)

    def _resolve_f_expression(self, f_expr):

        # try resolving the F expression; if it doesn't refer to a valid field or related field it will throw a FieldError
        expression = f_expr.resolve_expression(self.queryset.query)

        # extract the components of the F expression and do a sanity check
        lookups, parts, _ = self.queryset.query.solve_lookup_type(f_expr.name)
        assert len(lookups) == 1 and lookups[0] == "exact"  # F expression should not have qualifiers like __gt, __contains, etc

        # replace the last part of the reference with the target field name (e.g. this will replace `my_fkname` with `my_fkname_id`)
        parts[-1] = expression.target.get_attname()

        # join together the table name and field names to get a SQL-style reference to the target field
        return ".".join([self.queryset.model._meta.db_table] + parts)

    def _as_sql_reference(self, ref):
        if hasattr(ref, "id"):  # ref is a model instance; return its ID
            return ref.id
        elif isinstance(ref, string_types) or isinstance(ref, int):  # ref is a string or integer; assume it's an ID
            return ref
        elif isinstance(ref, F):  # ref is an F expression; resolve it to a SQL reference
            return self._resolve_f_expression(ref)
        else:
            raise Exception("Not a valid reference: %r" % ref)

    def filter_by_hierarchy(self,
                            source_user=None,
                            role_kind=None,
                            ancestor_collection=None,
                            descendant_collection=None,
                            target_user=None):
        """
        Filters a queryset through a multi-table join through the Collection hierarchy and Roles/Collections.

        To anchor the hierarchy model relations back into the main queryset itself, use F expressions. For example, if
        you are filtering on a FacilityUser queryset, and want to return all users that have an admin role for
        collection `mycoll`, you would use something like:
        `FacilityUser.objects.filter_by_hierarchy(source_user=F("id"), role_kind=ADMIN, descendant_collection=mycoll)`
        (Here, `source_user=F("id")` means that the id of the source user is the same as the id of the model being filtered,
        i.e. we're "filtering over source users" in the hierarchy structure.)

        :param source_user: a specific value, or F expression, to constrain the source FacilityUser in the hierarchy structure
        :param role_kind: a specific value, or F expression, to constrain the Role kind in the hierarchy structure
        :param ancestor_collection: a specific value, or F expression, to constrain the ancestor Collection in the hierarchy structure
        :param descendant_collection: a specific value, or F expression, to constrain the descendant Collection in the hierarchy structure
        :param target_user: a specific value, or F expression, to constrain the target FacilityUser in the hierarchy structure
        :return: a filtered queryset with all the hierarchy structure conditions applied, as well as conditions based on provided arguments
        :rtype: QuerySet
        """

        queryset = self.queryset

        ################################################################################################################
        # 1. First, determine which components of the hierarchy tree are relevant to the current query, and add in the
        # corresponding tables and base conditions to establish the relationships between them.
        ################################################################################################################

        # 1(a). If needed, add in the SQL to establish the relationships between the target user (member) and the collections.
        # (NOTE: This part should be kept first, before other "extras" apply, to avoid duplication of SQL by the "|")
        if target_user:  # there are two ways for the target user to be a member of the ancestor collection:
            # the first way is via the collection hierarchy; having a Membership for the descendant collection
            member_via_hierarchy = self._add_extras(queryset, **self._membership_extra)
            # the second, if the ancestor collection is the facility, is by virtue of being associated with that facility
            where_clause = [
                "ancestor_collection.kind = '{facility_kind}'".format(facility_kind=collection_kinds.FACILITY),
                "ancestor_collection.dataset_id = target_user.dataset_id",
            ]
            member_via_facility = self._add_extras(queryset, where=where_clause)
            # join the two querysets together with "|", so that either one will be matched
            queryset = member_via_facility | member_via_hierarchy

        # 1(b). Add the tables and conditions relating the ancestor and descendant collections to one another:
        queryset = self._add_extras(queryset, **self._collection_extra)

        # 1(c). If needed, add the tables for source FacilityUser and Role, and conditions linking them together:
        if source_user or role_kind:
            queryset = self._add_extras(queryset, **self._role_extra)

        ################################################################################################################
        # 2. Next, add in the additional conditions that apply constraints on the tables in the hierarchy, fixing their
        # fields to particular values or tying them into a field on the base table that is being queried.
        ################################################################################################################

        if source_user:
            where_clause = ['source_user.id = {id}'.format(id=self._as_sql_reference(source_user))]
            queryset = self._add_extras(queryset, where=where_clause)

        if role_kind:
            # if role_kind is a single string, put it into a list
            if isinstance(role_kind, string_types):
                role_kind = [role_kind]
            # convert the list of kinds into a list of strings for use in SQL
            kinds_string = "('{kind_list}')".format(kind_list="','".join(role_kind))
            where_clause = ['role.kind IN {kinds}'.format(kinds=kinds_string)]
            queryset = self._add_extras(queryset, where=where_clause)

        if ancestor_collection:
            where_clause = ['ancestor_collection.id = {id}'.format(id=self._as_sql_reference(ancestor_collection))]
            queryset = self._add_extras(queryset, where=where_clause)

        if descendant_collection:
            where_clause = ['descendant_collection.id = {id}'.format(id=self._as_sql_reference(descendant_collection))]
            queryset = self._add_extras(queryset, where=where_clause)

        if target_user:
            where_clause = ['target_user.id = {id}'.format(id=self._as_sql_reference(target_user))]
            queryset = self._add_extras(queryset, where=where_clause)

        return queryset
