from django.db.models import Q
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from sentry import analytics
from sentry.api.base import region_silo_endpoint
from sentry.api.bases.organization import OrganizationEndpoint, OrganizationSearchPermission
from sentry.api.serializers import serialize
from sentry.models.savedsearch import SavedSearch, SortOptions, Visibility
from sentry.models.search_common import SearchType


class OrganizationSearchSerializer(serializers.Serializer):
    type = serializers.IntegerField(required=True)
    name = serializers.CharField(required=True)
    query = serializers.CharField(required=True, min_length=1)
    sort = serializers.ChoiceField(
        choices=SortOptions.as_choices(), default=SortOptions.DATE, required=False
    )
    # TODO(epurkhiser): Once the frontend is deployed we should change this to
    # default to OWNER since that is a more sane default than organization
    # visibile.
    visibility = serializers.ChoiceField(
        choices=Visibility.as_choices(include_pinned=False),
        default=Visibility.ORGANIZATION,
        required=False,
    )


@region_silo_endpoint
class OrganizationSearchesEndpoint(OrganizationEndpoint):
    permission_classes = (OrganizationSearchPermission,)

    def get(self, request: Request, organization) -> Response:
        """
        List an Organization's saved searches
        `````````````````````````````````````
        Retrieve a list of saved searches for a given Organization. For custom
        saved searches, return them for all projects even if we have duplicates.
        For default searches, just return one of each search

        :auth: required

        """
        try:
            search_type = SearchType(int(request.GET.get("type", 0)))
        except ValueError as e:
            return Response({"detail": "Invalid input for `type`. Error: %s" % str(e)}, status=400)

        query = (
            SavedSearch.objects
            # Do not include pinned or personal searches from other users in
            # the same organization. DOES include the requesting users pinned
            # search
            .exclude(
                ~Q(owner=request.user),
                visibility__in=(Visibility.OWNER, Visibility.OWNER_PINNED),
            )
            .filter(
                Q(organization=organization) | Q(is_global=True),
                type=search_type,
            )
            .extra(order_by=["name"])
        )

        return Response(serialize(list(query), request.user))

    def post(self, request: Request, organization) -> Response:
        serializer = OrganizationSearchSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        result = serializer.validated_data

        # Prevent from creating duplicate queries
        if (
            SavedSearch.objects
            # Query duplication for pinned searches is fine, exlcuded these
            .exclude(visibility=Visibility.OWNER_PINNED)
            .filter(Q(is_global=True) | Q(organization=organization), query=result["query"])
            .exists()
        ):
            return Response(
                {"detail": "Query {} already exists".format(result["query"])}, status=400
            )

        saved_search = SavedSearch.objects.create(
            organization=organization,
            owner=request.user,
            type=result["type"],
            name=result["name"],
            query=result["query"],
            sort=result["sort"],
            visibility=result["visibility"],
        )
        analytics.record(
            "organization_saved_search.created",
            search_type=SearchType(saved_search.type).name,
            org_id=organization.id,
            query=saved_search.query,
        )
        return Response(serialize(saved_search, request.user))
