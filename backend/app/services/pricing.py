from functools import lru_cache
from datetime import datetime, timedelta, timezone
import asyncio

import boto3
from botocore.config import Config

FALLBACK_PRICES = {
    "t3.medium": 0.12,
    "t3.large": 0.1138,
    "t3a.large": 0.0986,
    "t3.xlarge": 0.2528,
    "t3a.xlarge": 0.1722,
    "c5a.xlarge": 0.33,
    "c6a.xlarge": 0.2775,
    "c6i.xlarge": 0.34,
    "c5.xlarge": 0.35,
    "m5a.xlarge": 0.32,
    "m5.large": 0.20,
    "m5.xlarge": 0.34,
    "m6a.large": 0.19,
    "m6a.xlarge": 0.32,
    "m6i.xlarge": 0.34,
    "r6a.xlarge": 0.38,
}

REGION_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-2": "US West (Oregon)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "eu-west-1": "EU (Ireland)",
}


def fallback_windows_price(instance_type: str) -> float:
    return FALLBACK_PRICES.get(instance_type, 0.1)


@lru_cache(maxsize=256)
def _cached_price(region: str, instance_type: str, bucket: int) -> float:
    del bucket
    client = boto3.client(
        "pricing",
        region_name="us-east-1",
        config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1}),
    )
    location = REGION_LOCATION.get(region)
    if not location:
        return FALLBACK_PRICES.get(instance_type, 0.1)
    response = client.get_products(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Windows"},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "No License required"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        ],
        MaxResults=1,
    )
    if not response.get("PriceList"):
        return FALLBACK_PRICES.get(instance_type, 0.1)
    import json

    product = json.loads(response["PriceList"][0])
    terms = product["terms"]["OnDemand"]
    first_term = next(iter(terms.values()))
    first_dimension = next(iter(first_term["priceDimensions"].values()))
    return float(first_dimension["pricePerUnit"]["USD"])


async def get_hourly_windows_price(region: str, instance_type: str, ttl_seconds: int) -> float:
    bucket = int(datetime.now(timezone.utc).timestamp() // max(ttl_seconds, 1))
    try:
        return await asyncio.to_thread(_cached_price, region, instance_type, bucket)
    except Exception:
        return FALLBACK_PRICES.get(instance_type, 0.1)


@lru_cache(maxsize=256)
def _cached_spot_price(region: str, instance_type: str, bucket: int) -> float | None:
    del bucket
    client = boto3.client(
        "ec2",
        region_name=region,
        config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}),
    )
    end = datetime.now(timezone.utc)
    response = client.describe_spot_price_history(
        InstanceTypes=[instance_type],
        ProductDescriptions=["Windows"],
        StartTime=end - timedelta(hours=6),
        EndTime=end,
        MaxResults=20,
    )
    prices = [float(item["SpotPrice"]) for item in response.get("SpotPriceHistory", [])]
    return min(prices) if prices else None


async def get_estimated_spot_windows_price(region: str, instance_type: str, ttl_seconds: int) -> float | None:
    bucket = int(datetime.now(timezone.utc).timestamp() // max(ttl_seconds, 1))
    try:
        return await asyncio.to_thread(_cached_spot_price, region, instance_type, bucket)
    except Exception:
        return None
