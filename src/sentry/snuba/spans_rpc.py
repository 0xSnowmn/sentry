import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

import sentry_sdk
from sentry_protos.snuba.v1.endpoint_time_series_pb2 import TimeSeries, TimeSeriesRequest
from sentry_protos.snuba.v1.trace_item_attribute_pb2 import AttributeAggregation, AttributeKey
from sentry_protos.snuba.v1.trace_item_filter_pb2 import AndFilter, OrFilter, TraceItemFilter

from sentry.api.event_search import SearchFilter, SearchKey, SearchValue
from sentry.exceptions import InvalidSearchQuery
from sentry.search.eap.constants import MAX_ROLLUP_POINTS, VALID_GRANULARITIES
from sentry.search.eap.resolver import SearchResolver
from sentry.search.eap.span_columns import SPAN_DEFINITIONS
from sentry.search.eap.types import CONFIDENCES, EAPResponse, SearchResolverConfig
from sentry.search.events.fields import get_function_alias, is_function
from sentry.search.events.types import SnubaData, SnubaParams
from sentry.snuba import rpc_dataset_common
from sentry.snuba.discover import OTHER_KEY, create_result_key, zerofill
from sentry.utils import snuba_rpc
from sentry.utils.snuba import SnubaTSResult, process_value

logger = logging.getLogger("sentry.snuba.spans_rpc")


def get_resolver(params: SnubaParams, config: SearchResolverConfig) -> SearchResolver:
    return SearchResolver(
        params=params,
        config=config,
        definitions=SPAN_DEFINITIONS,
    )


@sentry_sdk.trace
def run_table_query(
    params: SnubaParams,
    query_string: str,
    selected_columns: list[str],
    orderby: list[str] | None,
    offset: int,
    limit: int,
    referrer: str,
    config: SearchResolverConfig,
    search_resolver: SearchResolver | None = None,
):
    return rpc_dataset_common.run_table_query(
        query_string,
        selected_columns,
        orderby,
        offset,
        limit,
        referrer,
        search_resolver or get_resolver(params, config),
    )


def get_timeseries_query(
    params: SnubaParams,
    query_string: str,
    y_axes: list[str],
    groupby: list[str],
    referrer: str,
    config: SearchResolverConfig,
    granularity_secs: int,
    extra_conditions: TraceItemFilter | None = None,
) -> TimeSeriesRequest:
    resolver = get_resolver(params=params, config=config)
    meta = resolver.resolve_meta(referrer=referrer)
    query, _, query_contexts = resolver.resolve_query(query_string)
    (aggregations, _) = resolver.resolve_aggregates(y_axes)
    (groupbys, _) = resolver.resolve_columns(groupby)
    if extra_conditions is not None:
        if query is not None:
            query = TraceItemFilter(and_filter=AndFilter(filters=[query, extra_conditions]))
        else:
            query = extra_conditions

    return TimeSeriesRequest(
        meta=meta,
        filter=query,
        aggregations=[
            agg.proto_definition
            for agg in aggregations
            if isinstance(agg.proto_definition, AttributeAggregation)
        ],
        group_by=[
            groupby.proto_definition
            for groupby in groupbys
            if isinstance(groupby.proto_definition, AttributeKey)
        ],
        granularity_secs=granularity_secs,
    )


def validate_granularity(
    params: SnubaParams,
    granularity_secs: int,
) -> None:
    """The granularity has already been somewhat validated by src/sentry/utils/dates.py:validate_granularity
    but the RPC adds additional rules on validation so those are checked here"""
    if params.date_range.total_seconds() / granularity_secs > MAX_ROLLUP_POINTS:
        raise InvalidSearchQuery(
            "Selected interval would create too many buckets for the timeseries"
        )
    if granularity_secs not in VALID_GRANULARITIES:
        raise InvalidSearchQuery(
            f"Selected interval is not allowed, allowed intervals are: {sorted(VALID_GRANULARITIES)}"
        )


@sentry_sdk.trace
def run_timeseries_query(
    params: SnubaParams,
    query_string: str,
    y_axes: list[str],
    referrer: str,
    granularity_secs: int,
    config: SearchResolverConfig,
    comparison_delta: timedelta | None = None,
) -> SnubaTSResult:
    """Make the query"""
    validate_granularity(params, granularity_secs)
    rpc_request = get_timeseries_query(
        params, query_string, y_axes, [], referrer, config, granularity_secs
    )

    """Run the query"""
    rpc_response = snuba_rpc.timeseries_rpc([rpc_request])[0]

    """Process the results"""
    result: SnubaData = []
    confidences: SnubaData = []
    for timeseries in rpc_response.result_timeseries:
        processed, confidence = _process_all_timeseries([timeseries], params, granularity_secs)
        if len(result) == 0:
            result = processed
            confidences = confidence
        else:
            for existing, new in zip(result, processed):
                existing.update(new)
            for existing, new in zip(confidences, confidence):
                existing.update(new)
    if len(result) == 0:
        # The rpc only zerofills for us when there are results, if there aren't any we have to do it ourselves
        result = zerofill(
            [],
            params.start_date,
            params.end_date,
            granularity_secs,
            ["time"],
        )

    if comparison_delta is not None:
        if len(rpc_request.aggregations) != 1:
            raise InvalidSearchQuery("Only one column can be selected for comparison queries")

        comp_query_params = params.copy()
        assert comp_query_params.start is not None, "start is required"
        assert comp_query_params.end is not None, "end is required"
        comp_query_params.start = comp_query_params.start_date - comparison_delta
        comp_query_params.end = comp_query_params.end_date - comparison_delta

        comp_rpc_request = get_timeseries_query(
            comp_query_params, query_string, y_axes, [], referrer, config, granularity_secs
        )
        comp_rpc_response = snuba_rpc.timeseries_rpc([comp_rpc_request])[0]

        if comp_rpc_response.result_timeseries:
            timeseries = comp_rpc_response.result_timeseries[0]
            processed, _ = _process_all_timeseries([timeseries], params, granularity_secs)
            label = get_function_alias(timeseries.label)
            for existing, new in zip(result, processed):
                existing["comparisonCount"] = new[label]
        else:
            for existing in result:
                existing["comparisonCount"] = 0

    return SnubaTSResult(
        {"data": result, "confidence": confidences}, params.start, params.end, granularity_secs
    )


@sentry_sdk.trace
def build_top_event_conditions(
    resolver: SearchResolver, top_events: EAPResponse, groupby_columns: list[str]
) -> Any:
    conditions = []
    other_conditions = []
    for event in top_events["data"]:
        row_conditions = []
        other_row_conditions = []
        for key in groupby_columns:
            if key == "project.id":
                value = resolver.params.project_slug_map[
                    event.get("project", event.get("project.slug"))
                ]
            else:
                value = event[key]
            resolved_term, context = resolver.resolve_term(
                SearchFilter(
                    key=SearchKey(name=key),
                    operator="=",
                    value=SearchValue(raw_value=value),
                )
            )
            if resolved_term is not None:
                row_conditions.append(resolved_term)
            other_term, context = resolver.resolve_term(
                SearchFilter(
                    key=SearchKey(name=key),
                    operator="!=",
                    value=SearchValue(raw_value=value),
                )
            )
            if other_term is not None:
                other_row_conditions.append(other_term)
        conditions.append(TraceItemFilter(and_filter=AndFilter(filters=row_conditions)))
        other_conditions.append(TraceItemFilter(or_filter=OrFilter(filters=other_row_conditions)))
    return (
        TraceItemFilter(or_filter=OrFilter(filters=conditions)),
        TraceItemFilter(and_filter=AndFilter(filters=other_conditions)),
    )


def run_top_events_timeseries_query(
    params: SnubaParams,
    query_string: str,
    y_axes: list[str],
    raw_groupby: list[str],
    orderby: list[str] | None,
    limit: int,
    referrer: str,
    granularity_secs: int,
    config: SearchResolverConfig,
) -> Any:
    """We intentionally duplicate run_timeseries_query code here to reduce the complexity of needing multiple helper
    functions that both would call
    This is because at time of writing, the query construction is very straightforward, if that changes perhaps we can
    change this"""
    """Make a table query first to get what we need to filter by"""
    validate_granularity(params, granularity_secs)
    search_resolver = get_resolver(params, config)
    top_events = run_table_query(
        params,
        query_string,
        raw_groupby + y_axes,
        orderby,
        0,
        limit,
        referrer,
        config,
        search_resolver,
    )
    if len(top_events["data"]) == 0:
        return {}
    # Need to change the project slug columns to project.id because timeseries requests don't take virtual_column_contexts
    groupby_columns = [col for col in raw_groupby if not is_function(col)]
    groupby_columns_without_project = [
        col if col not in ["project", "project.name"] else "project.id" for col in groupby_columns
    ]
    top_conditions, other_conditions = build_top_event_conditions(
        search_resolver, top_events, groupby_columns_without_project
    )
    """Make the query"""
    rpc_request = get_timeseries_query(
        params,
        query_string,
        y_axes,
        groupby_columns_without_project,
        referrer,
        config,
        granularity_secs,
        extra_conditions=top_conditions,
    )
    other_request = get_timeseries_query(
        params,
        query_string,
        y_axes,
        groupby_columns_without_project,
        referrer,
        config,
        granularity_secs,
        extra_conditions=other_conditions,
    )

    """Run the query"""
    rpc_response, other_response = snuba_rpc.timeseries_rpc([rpc_request, other_request])

    """Process the results"""
    map_result_key_to_timeseries = defaultdict(list)
    for timeseries in rpc_response.result_timeseries:
        groupby_attributes = timeseries.group_by_attributes
        remapped_groupby = {}
        # Remap internal attrs back to public ones
        for col in groupby_columns:
            if col in ["project", "project.slug"]:
                resolved_groupby, _ = search_resolver.resolve_attribute("project.id")
                remapped_groupby[col] = params.project_id_map[
                    int(groupby_attributes[resolved_groupby.internal_name])
                ]
            else:
                resolved_groupby, _ = search_resolver.resolve_attribute(col)
                remapped_groupby[col] = groupby_attributes[resolved_groupby.internal_name]
        result_key = create_result_key(remapped_groupby, groupby_columns, {})
        map_result_key_to_timeseries[result_key].append(timeseries)
    final_result = {}
    # Top Events actually has the order, so we need to iterate through it, regenerate the result keys
    for index, row in enumerate(top_events["data"]):
        result_key = create_result_key(row, groupby_columns, {})
        result_data, result_confidence = _process_all_timeseries(
            map_result_key_to_timeseries[result_key],
            params,
            granularity_secs,
        )
        final_result[result_key] = SnubaTSResult(
            {
                "data": result_data,
                "confidence": result_confidence,
                "order": index,
            },
            params.start,
            params.end,
            granularity_secs,
        )
    if other_response.result_timeseries:
        result_data, result_confidence = _process_all_timeseries(
            [timeseries for timeseries in other_response.result_timeseries],
            params,
            granularity_secs,
        )
        final_result[OTHER_KEY] = SnubaTSResult(
            {
                "data": result_data,
                "confidence": result_confidence,
                "order": limit,
            },
            params.start,
            params.end,
            granularity_secs,
        )
    return final_result


def _process_all_timeseries(
    all_timeseries: list[TimeSeries],
    params: SnubaParams,
    granularity_secs: int,
    order: int | None = None,
) -> tuple[SnubaData, SnubaData]:
    result: SnubaData = []
    confidence: SnubaData = []

    for timeseries in all_timeseries:
        # Timeseries serialization expects the function alias (eg. `count` not `count()`)
        label = get_function_alias(timeseries.label)
        if result:
            for index, bucket in enumerate(timeseries.buckets):
                assert result[index]["time"] == bucket.seconds
                assert confidence[index]["time"] == bucket.seconds
        else:
            for bucket in timeseries.buckets:
                result.append({"time": bucket.seconds})
                confidence.append({"time": bucket.seconds})

        for index, data_point in enumerate(timeseries.data_points):
            result[index][label] = process_value(data_point.data)
            confidence[index][label] = CONFIDENCES.get(data_point.reliability, None)

    return result, confidence
