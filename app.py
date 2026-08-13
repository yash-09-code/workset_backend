import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
logger = logging.getLogger(__name__)
app = FastAPI(title="Business API", version="1.0.0")
bearer_scheme = HTTPBearer(auto_error=False)
WEB_DIR = Path(__file__).parent / "web"

# Every resource below belongs to exactly one company.  The API writes the
# company id from the login token rather than trusting an id supplied by a client.
COMPANY_TABLES = frozenset(
    {
        "categories", "product", "employee", "customers", "suppliers",
        "sales", "expenses", "inventory", "purchase",
    }
)
RESOURCES = COMPANY_TABLES | {"sale_items", "notifications"}
FOREIGN_COMPANY_CHECKS = {
    "product": {"category_id": "categories"},
    "sales": {"customer_id": "customers", "employee_id": "employee"},
    "purchase": {"supplier_id": "suppliers"},
    "inventory": {"product_id": "product"},
    "sale_items": {"sale_id": "sales", "product_id": "product"},
}


class SignUpRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class SetupProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    company_name: str = Field(min_length=1, max_length=150)
    # Kept as ``disc`` to remain compatible with the existing frontend payload.
    disc: str = Field(default="", max_length=1_000)


@lru_cache
def get_supabase() -> Client:
    """Create the client only when an endpoint needs it.

    This keeps imports and local tests working even when environment variables
    have not yet been configured.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
        )
    return create_client(url, key)


def get_current_profile_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> int:
    """Resolve the authenticated Supabase user to the application's profile id."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    try:
        supabase = get_supabase()
        user_response = supabase.auth.get_user(credentials.credentials)
        user = user_response.user
        if user is None:
            raise ValueError("No user returned")

        profile_response = (
            supabase.table("profile").select("id").eq("auth_user_id", user.id).limit(1).execute()
        )
        if not profile_response.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
        return profile_response.data[0]["id"]
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not authenticate request")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def get_current_company(profile_id: int = Depends(get_current_profile_id)) -> dict[str, int]:
    """Return the profile and company attached to the bearer token."""
    try:
        response = get_supabase().table("profile").select("id,company_id").eq("id", profile_id).limit(1).execute()
        if not response.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
        company_id = response.data[0].get("company_id")
        if company_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Complete /set-up-profile before managing business data",
            )
        return {"profile_id": profile_id, "company_id": company_id}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not resolve company for profile %s", profile_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not resolve company")


def require_resource(resource: str) -> None:
    if resource not in RESOURCES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown resource")


def clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Prevent clients from changing primary keys and ownership columns."""
    blocked = {"id", "company_id", "owner_id", "auth_user_id", "user_id", "created_by"}
    return {key: value for key, value in payload.items() if key not in blocked}


def verify_foreign_keys(resource: str, payload: dict[str, Any], company_id: int) -> None:
    """Ensure related records cannot be borrowed from another company."""
    supabase = get_supabase()
    for column, foreign_table in FOREIGN_COMPANY_CHECKS.get(resource, {}).items():
        value = payload.get(column)
        if value is None:
            continue
        query = supabase.table(foreign_table).select("id").eq("id", value).limit(1)
        if foreign_table in COMPANY_TABLES:
            query = query.eq("company_id", company_id)
        if not query.execute().data:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {column}")


def resource_query(resource: str, company: dict[str, int]):
    # Supabase's Python client exposes filters (``eq``, ``in_``) on a select
    # builder, not directly on the table request builder.
    query = get_supabase().table(resource).select("*")
    if resource in COMPANY_TABLES:
        return query.eq("company_id", company["company_id"])
    if resource == "notifications":
        return query.eq("user_id", company["profile_id"])
    # Sale items inherit their company from the associated sale.
    return query


def find_resource(resource: str, record_id: int, company: dict[str, int]) -> dict[str, Any]:
    """Find one permitted record, including the indirect sale_items ownership check."""
    response = resource_query(resource, company).eq("id", record_id).limit(1).execute()
    if not response.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} record not found")
    record = response.data[0]
    if resource == "sale_items":
        sale = (
            get_supabase().table("sales").select("id").eq("id", record["sale_id"])
            .eq("company_id", company["company_id"]).limit(1).execute()
        )
        if not sale.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sale_items record not found")
    return record


@app.get("/")
def read_root():
    """Serve the business dashboard."""
    return FileResponse(WEB_DIR / "index.html")


@app.post("/sign-up", status_code=status.HTTP_201_CREATED)
def sign_up(request: SignUpRequest):
    try:
        supabase = get_supabase()
        auth_response = supabase.auth.sign_up({"email": request.email, "password": request.password})
        if auth_response.user is None:
            raise ValueError("Supabase did not return a user")

        profile_response = (
            supabase.table("profile")
            .insert(
                {
                    "email": request.email,
                    "role": "owner",
                    "auth_user_id": auth_response.user.id,
                    "name": "none",
                    "profile": 1,
                }
            )
            .execute()
        )
        if not profile_response.data:
            raise ValueError("Profile was not created")

        return {"message": "Profile created", "id": profile_response.data[0]["id"]}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Sign-up failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sign-up failed")


@app.post("/login")
def login(request: SignUpRequest):
    try:
        auth_response = get_supabase().auth.sign_in_with_password(
            {"email": request.email, "password": request.password}
        )
        if auth_response.session is None:
            raise ValueError("Supabase did not return a session")
        return {
            "message": "Login successful",
            "access_token": auth_response.session.access_token,
            "token_type": "bearer",
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Login failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")


@app.post("/set-up-profile")
def set_up_profile(request: SetupProfileRequest, profile_id: int = Depends(get_current_profile_id)):
    """Update the signed-in owner and create or update that owner's company."""
    try:
        supabase = get_supabase()
        supabase.table("profile").update({"name": request.name}).eq("id", profile_id).execute()

        # ``company.owner_id`` is a foreign key to Supabase's auth.users table,
        # which uses UUIDs.  The application's ``profile.id`` is an integer, so
        # it cannot be stored in that column.
        profile_response = (
            supabase.table("profile").select("auth_user_id").eq("id", profile_id).limit(1).execute()
        )
        if not profile_response.data or not profile_response.data[0].get("auth_user_id"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile authentication id not found")
        owner_auth_user_id = profile_response.data[0]["auth_user_id"]

        company_response = (
            supabase.table("company").select("id").eq("owner_id", owner_auth_user_id).limit(1).execute()
        )
        company_data = {
            "name": request.company_name,
            "description": request.disc,
            "owner_id": owner_auth_user_id,
        }
        if company_response.data:
            company_id = company_response.data[0]["id"]
            supabase.table("company").update(company_data).eq("id", company_id).execute()
        else:
            created_company = supabase.table("company").insert(company_data).execute()
            if not created_company.data:
                raise ValueError("Company was not created")
            company_id = created_company.data[0]["id"]

        supabase.table("profile").update({"company_id": company_id}).eq("id", profile_id).execute()
        return {"message": "Profile setup completed", "profile_id": profile_id, "company_id": company_id}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Profile setup failed for profile %s", profile_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Profile setup failed")


@app.get("/get-profile/{profile_id}")
def get_profile(profile_id: int, current_profile_id: int = Depends(get_current_profile_id)):
    if profile_id != current_profile_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to view this profile")
    try:
        response = get_supabase().table("profile").select("*").eq("id", profile_id).limit(1).execute()
        if not response.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
        return response.data[0]
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to retrieve profile %s", profile_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve profile")


@app.get("/me")
def get_my_profile(profile_id: int = Depends(get_current_profile_id)):
    """Return the signed-in user's profile."""
    return get_profile(profile_id, profile_id)


@app.patch("/me")
def update_my_profile(
    payload: dict[str, Any] = Body(...), profile_id: int = Depends(get_current_profile_id)
):
    """Update safe profile columns, for example name or phone."""
    data = clean_payload(payload)
    # These fields define authentication and company ownership and are never client-editable.
    data.pop("role", None)
    data.pop("profile", None)
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No updatable fields provided")
    try:
        response = get_supabase().table("profile").update(data).eq("id", profile_id).execute()
        return response.data[0] if response.data else {"message": "Profile updated"}
    except Exception:
        logger.exception("Failed to update profile %s", profile_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Profile update failed")


@app.get("/company")
def get_my_company(company: dict[str, int] = Depends(get_current_company)):
    response = get_supabase().table("company").select("*").eq("id", company["company_id"]).limit(1).execute()
    if not response.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return response.data[0]


@app.patch("/company")
def update_my_company(
    payload: dict[str, Any] = Body(...), company: dict[str, int] = Depends(get_current_company)
):
    data = clean_payload(payload)
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No updatable fields provided")
    try:
        response = get_supabase().table("company").update(data).eq("id", company["company_id"]).execute()
        return response.data[0] if response.data else {"message": "Company updated"}
    except Exception:
        logger.exception("Failed to update company %s", company["company_id"])
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Company update failed")


@app.get("/api/{resource}")
def list_records(
    resource: str,
    limit: int = Query(default=100, ge=1, le=500),
    company: dict[str, int] = Depends(get_current_company),
):
    """List records from any business table belonging to the current company."""
    require_resource(resource)
    try:
        if resource == "sale_items":
            sales = get_supabase().table("sales").select("id").eq("company_id", company["company_id"]).execute()
            sale_ids = [sale["id"] for sale in sales.data]
            return [] if not sale_ids else get_supabase().table(resource).select("*").in_("sale_id", sale_ids).limit(limit).execute().data
        return resource_query(resource, company).limit(limit).execute().data
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list %s", resource)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to list {resource}")


@app.post("/api/{resource}", status_code=status.HTTP_201_CREATED)
def create_record(
    resource: str,
    payload: dict[str, Any] = Body(...),
    company: dict[str, int] = Depends(get_current_company),
):
    """Create a record. Send your table's normal columns; ownership is filled automatically."""
    require_resource(resource)
    data = clean_payload(payload)
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Request body cannot be empty")
    verify_foreign_keys(resource, data, company["company_id"])
    if resource in COMPANY_TABLES:
        data["company_id"] = company["company_id"]
    elif resource == "notifications":
        data["user_id"] = company["profile_id"]
    try:
        response = get_supabase().table(resource).insert(data).execute()
        if not response.data:
            raise ValueError("Insert returned no record")
        return response.data[0]
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to create %s", resource)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to create {resource}")


@app.get("/api/{resource}/{record_id}")
def get_record(resource: str, record_id: int, company: dict[str, int] = Depends(get_current_company)):
    require_resource(resource)
    return find_resource(resource, record_id, company)


@app.patch("/api/{resource}/{record_id}")
def update_record(
    resource: str,
    record_id: int,
    payload: dict[str, Any] = Body(...),
    company: dict[str, int] = Depends(get_current_company),
):
    require_resource(resource)
    find_resource(resource, record_id, company)
    data = clean_payload(payload)
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No updatable fields provided")
    verify_foreign_keys(resource, data, company["company_id"])
    try:
        response = get_supabase().table(resource).update(data).eq("id", record_id).execute()
        return response.data[0] if response.data else {"message": f"{resource} updated"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update %s %s", resource, record_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to update {resource}")


@app.delete("/api/{resource}/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(resource: str, record_id: int, company: dict[str, int] = Depends(get_current_company)):
    require_resource(resource)
    find_resource(resource, record_id, company)
    try:
        get_supabase().table(resource).delete().eq("id", record_id).execute()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to delete %s %s", resource, record_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to delete {resource}")
