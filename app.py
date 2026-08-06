from supabase import create_client, Client
from fastapi import FastAPI ,HTTPException

from pydantic import BaseModel

app = FastAPI()

SUPABASE_URL = "https://rfpdoyltsvruxbofcwob.supabase.co"

supabase: Client = create_client(SUPABASE_URL, "sb_secret_sq3v0_WFm-ECB9iJEloyAg_AYQxeT7j")



class SignUpRequest(BaseModel):
    email: str
    password: str

class setUpProfileRequest(BaseModel):
    id: int
    name: str
    company_name: str
    disc: str = ""



@app.get("/")
def read_root():
    return {"message": "Welcome to the FastAPI Supabase example!"}

#profile 1 or sign-up
@app.post("/sign-up")
def sign_up(sign_up_request: SignUpRequest):
    try:
        response = supabase.auth.sign_up({
            "email": sign_up_request.email,
            "password": sign_up_request.password,
        })

        print(response)

        id = response.user.id
        data = {
            "email": sign_up_request.email,
            "role": "owner",
            "auth_user_id": id,
            "name": "none",
            "profile": 1
        }
        print(supabase.table("profile").insert(data).execute())

        response = supabase.table("profile").select("id").eq("auth_user_id", id).execute()
        print(response)

        return {"message": "PROFILE 1 CREATED", "id": response.data[0]["id"]}

    except Exception as e:
        raise HTTPException(status_code=400, detail="Sign-up failed")

#login or get data
@app.post("/login")
def login(login_request: SignUpRequest):
    try:
        session_data = supabase.auth.sign_in_with_password({
            "email": login_request.email,
            "password": login_request.password,
        })
        print("Login successful!")
        
        # Access the access token or user details
        access_token = session_data.session.access_token
        return {"message": "Login successful!", "access_token": access_token}
    
    except Exception as e:
        raise HTTPException(status_code=400, detail="Login failed")




@app.post("/set-up-profile")
def set_up_profile(set_up_profile_request: setUpProfileRequest):
    try:
        data = {
            "name": set_up_profile_request.name,
        }

        response = supabase.table("profile").update(data).eq("id", set_up_profile_request.id).execute()
        print("Profile data updated in the 'profile' table.")

        data = {
            "name": set_up_profile_request.company_name,
            "description": set_up_profile_request.disc,
            "owner_id": set_up_profile_request.id
        }

        response = supabase.table("company").insert(data).execute()
        print("Company data inserted into the 'company' table.")

        id_c = supabase.table("company").select("id").where("owner_id", "eq", set_up_profile_request.id).execute()

        data = {
            "company_id": id_c[0]["id"]
        }

        response = supabase.table("profile").update(data).eq("id", set_up_profile_request.id).execute()
        print("Profile data updated in the 'profile' table.")

    except Exception as e:
        raise HTTPException(status_code=400, detail="Profile setup failed")



@app.get("/get-profile/{profile_id}")
def get_profile(profile_id: int):
    try:
        response = supabase.table("profile").select("*").eq("id", profile_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Profile not found")
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail="Failed to retrieve profile")






