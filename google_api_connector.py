import aiohttp
import logging
from typing import List, Dict, Any, Tuple, Optional
import json
import asyncio
from fastapi import HTTPException
import requests
from all_types.myapi_dtypes import ReqStreeViewCheck, ReqFetchDataset
from backend_common.utils.utils import convert_strings_to_ints
from config_factory import CONF
from backend_common.logging_wrapper import apply_decorator_to_module
from all_types.response_dtypes import (
    LegInfo,
    TrafficCondition,
    RouteInfo,
)
from boolean_query_processor import optimize_query_sequence
from mapbox_connector import MapBoxConnector
from storage import load_dataset, make_dataset_filename, make_dataset_filename_part, store_data_resp
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)



# Load and flatten the popularity data
with open("Backend/ggl_categories_poi_estimate.json", "r") as f:
    raw_popularity_data = json.load(f)

# Flatten the nested dictionary - we only care about subkeys
POPULARITY_DATA = {}
for category in raw_popularity_data.values():
    POPULARITY_DATA.update(category)


async def fetch_from_google_maps_api(req: ReqFetchDataset) -> Tuple[List[Dict[str, Any]], str]:
    try:

        combined_dataset_id = make_dataset_filename(req)
        existing_combined_data = await load_dataset(combined_dataset_id)
        
        if existing_combined_data:
            logger.info(f"Returning existing combined dataset: {combined_dataset_id}")
            return existing_combined_data
        optimized_queries = optimize_query_sequence(req.boolean_query, POPULARITY_DATA)

        datasets = {}
        missing_queries = []
        

        for included_types, excluded_types in optimized_queries:
            full_dataset_id = make_dataset_filename_part(req, included_types, excluded_types)
            stored_data = await load_dataset(full_dataset_id)

            if stored_data:
                datasets[full_dataset_id] = stored_data
            else:
                missing_queries.append((full_dataset_id, included_types, excluded_types))

        if missing_queries:
            logger.info(f"Fetching {len(missing_queries)} queries from Google Maps API.")
            query_tasks = [
                execute_single_query(req, included_types, excluded_types)
                for _, included_types, excluded_types in missing_queries
            ]

            all_query_results = await asyncio.gather(*query_tasks)

            for (dataset_id, included, excluded), query_results in zip(missing_queries, all_query_results):
                    
                if query_results:  
                    dataset = await MapBoxConnector.new_ggl_to_boxmap(query_results,req.radius)
                    dataset = convert_strings_to_ints(dataset)
                    await store_data_resp(req, dataset, dataset_id)
                    datasets[dataset_id] = dataset

        # Initialize the combined dictionary
        combined = {
            'type': 'FeatureCollection',
            'features': [],
            'properties': set()
        }

        # Initialize a set to keep track of unique IDs
        seen_ids = set()

        # Iterate through each dataset
        for dataset in datasets.values():
            # Add properties to the combined set
            combined['properties'].update(dataset.get('properties', []))
            features = dataset.get('features', [])
            
            # Iterate through each feature in the dataset
            for feature in features:
                feature_id = feature.get('properties', {}).get('id')
                if feature_id is not None and feature_id not in seen_ids:
                    combined['features'].append(feature)
                    seen_ids.add(feature_id)

        # Convert the properties set back to a list (if needed)
        combined['properties'] = list(combined['properties'])

        if combined:
            await store_data_resp(req, combined, combined_dataset_id)
            for feature in combined['features']:
                if 'properties' in feature and 'id' in feature['properties']:
                    del feature['properties']['id']
            logger.info(f"Stored combined dataset: {combined_dataset_id}")
            return combined
        else:
            logger.warning("No valid results returned from Google Maps API or DB.")
            return combined

    except Exception as e:
        logger.error(f"Error in fetch_from_google_maps_api: {str(e)}")
        return str(e)


async def execute_single_query(
    location_data: ReqFetchDataset, included_types: List[str], excluded_types: List[str]
) -> List[dict]:
    data = {
        "includedTypes": included_types,
        "excludedTypes": excluded_types,
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": location_data.lat,
                    "longitude": location_data.lng,
                },
                "radius": location_data.radius,
            }
        },
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": CONF.api_key,
        "X-Goog-FieldMask": CONF.google_fields,
    }

    try:
        async with aiohttp.ClientSession() as session:
            logger.debug(
                f"Executing query - Include: {included_types}, Exclude: {excluded_types}"
            )
            async with session.post(
                CONF.nearby_search, headers=headers, json=data
            ) as response:
                if response.status == 200:
                    response_data = await response.json()
                    results = response_data.get("places", [])
                    logger.debug(f"Query returned {len(results)} results")
                    return results
                else:
                    error_msg = await response.text()
                    logger.error(f"API request failed: {error_msg}")
                    return []

    except aiohttp.ClientError as e:
        # TODO this doesn't reraise the error, not sure what to do about it
        logger.error(f"Network error during API request: {str(e)}")
        return []





async def text_fetch_from_google_maps_api(req: ReqFetchDataset) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": CONF.api_key,
        "X-Goog-FieldMask": CONF.google_fields+",nextPageToken",
    }
    data = {
        "textQuery": req.text_search,
        "includePureServiceAreaBusinesses": False,
        "pageToken": req.page_token,
        "locationBias": {
            "circle": {
                "center": {"latitude": req.lat, "longitude": req.lng},
                "radius": req.radius,
            }
        },
    }
    response = requests.post(CONF.search_text, headers=headers, json=data)
    if response.status_code == 200:
        response_data = response.json()
        results = response_data.get("places", [])
        next_page_token = response_data.get("nextPageToken", "")
        return results, next_page_token
    else:
        print("Error:", response.status_code, response.text)
        return [], None


async def check_street_view_availability(req: ReqStreeViewCheck) -> Dict[str, bool]:
    url = f"https://maps.googleapis.com/maps/api/streetview?return_error_code=true&size=600x300&location={req.lat},{req.lng}&heading=151.78&pitch=-0.76&key={CONF.api_key}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return {"has_street_view": True}
            else:
                raise HTTPException(
                    status_code=499,
                    detail=f"Error checking Street View availability, error = {response.status}",
                )


async def calculate_distance_traffic_route(
    origin: str, destination: str
) -> RouteInfo:  # GoogleApi connector
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": origin.split(",")[0],
                    "longitude": origin.split(",")[1],
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": destination.split(",")[0],
                    "longitude": destination.split(",")[1],
                }
            }
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
        "extraComputations": ["TRAFFIC_ON_POLYLINE"],
        "polylineQuality": "high_quality",
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": CONF.api_key,
        "X-Goog-fieldmask": "*",
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        if "routes" not in response_data:
            raise HTTPException(status_code=400, detail="No route found.")

        # Parse the first route's leg for necessary details
        route_info = []
        for leg in response_data["routes"][0]["legs"]:
            leg_info = LegInfo(
                start_location=leg["startLocation"],
                end_location=leg["endLocation"],
                distance=leg["distanceMeters"],
                duration=leg["duration"],
                static_duration=leg["staticDuration"],
                polyline=leg["polyline"]["encodedPolyline"],
                traffic_conditions=[
                    TrafficCondition(
                        start_index=interval.get("startPolylinePointIndex", 0),
                        end_index=interval["endPolylinePointIndex"],
                        speed=interval["speed"],
                    )
                    for interval in leg["travelAdvisory"].get(
                        "speedReadingIntervals", []
                    )
                ],
            )
            route_info.append(leg_info)

        return RouteInfo(origin=origin, destination=destination, route=route_info)

    except requests.RequestException:
        raise HTTPException(
            status_code=400,
            detail="Error fetching route information from Google Maps API",
        )


# Apply the decorator to all functions in this module
apply_decorator_to_module(logger)(__name__)
