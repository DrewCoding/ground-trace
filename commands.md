aws cognito-idp admin-create-user --user-pool-id us-west-1_lAHn3kSk5 --username "drewwilliams9090@gmail.com" --user-attributes "Name=email,Value=drewwilliams9090@gmail.com" "Name=email_verified,Value=true" --region us-west-1

{  
 "User": {  
 "Username": "09d9a95e-d091-707b-2b34-2c80b06e85e8",
"Attributes": [
{
"Name": "email",
"Value": "drewwilliams9090@gmail.com"
},
{
"Name": "email_verified",
"Value": "true"
 },
 {
 "Name": "sub",
 "Value": "09d9a95e-d091-707b-2b34-2c80b06e85e8"
 }
 ],  
 "UserCreateDate": "2026-10-01T23:22:38.922000-07:00",  
 "UserLastModifiedDate": "2026-10-01T23:22:38.922000-07:00",  
 "Enabled": true,  
 "UserStatus": "FORCE_CHANGE_PASSWORD"  
 }  
}
