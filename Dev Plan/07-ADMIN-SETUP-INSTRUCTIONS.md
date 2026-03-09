# Microsoft Entra ID App Registration — Admin Setup Instructions

## Who Is This For

This document is for the **Microsoft Entra ID (Azure AD) administrator** at UCSH. It contains step-by-step instructions to create an App Registration that our internal leave management server needs to operate.

## Background

We are migrating our leave request system off of Power Automate onto a self-hosted server. The server needs to:

- **Read and write SharePoint lists** on the `UCSHBulletinBoard` site (employee records, leave requests, overtime requests, etc.)
- **Read and write files** in SharePoint document libraries (monthly reports, CSV exports)
- **Send emails** as `hr@ucshca.onmicrosoft.com` (approval requests, notifications, balance updates)

The server runs 24/7 without a user logged in, so it authenticates as an **application** (not as a user). This requires an App Registration with application-level permissions and admin consent.

---

## Step 1: Create the App Registration

1. Go to [Microsoft Entra admin center](https://entra.microsoft.com)
2. Navigate to **Identity** → **Applications** → **App registrations**
3. Click **+ New registration**
4. Fill in:
   - **Name:** `UCSH Leave Management Server`
   - **Supported account types:** "Accounts in this organizational directory only" (Single tenant)
   - **Redirect URI:** Leave blank (not needed for client credentials flow)
5. Click **Register**

### Record These Values

After creation, you'll land on the app's Overview page. Copy and provide these to the development team:

| Value | Where to Find It |
|-------|-----------------|
| **Application (client) ID** | Overview page, top section |
| **Directory (tenant) ID** | Overview page, top section (should be `3f51285e-5594-4af6-869b-fe72b32298fd`) |

---

## Step 2: Create a Client Secret

1. In the app registration, go to **Certificates & secrets**
2. Click **+ New client secret**
3. Fill in:
   - **Description:** `Railway Production`
   - **Expires:** 24 months (or per your organization's policy)
4. Click **Add**
5. **Immediately copy the secret Value** (it will only be shown once). Provide this to the development team securely (not via email — use a password manager, Teams message, or in-person).

| Value | Where to Find It |
|-------|-----------------|
| **Client Secret** | The "Value" column (NOT the "Secret ID") — copy it immediately after creation |

> **Important:** Set a calendar reminder to rotate this secret before it expires. When it expires, the server will stop functioning until a new secret is generated and deployed.

---

## Step 3: Add API Permissions

1. In the app registration, go to **API permissions**
2. Click **+ Add a permission**
3. Select **Microsoft Graph**
4. Select **Application permissions** (not Delegated)
5. Search for and add each of these permissions:

| Permission | Purpose |
|-----------|---------|
| `Sites.ReadWrite.All` | Read and write all SharePoint list items (employee records, leave requests, overtime requests, holiday data) |
| `Mail.Send` | Send emails as hr@ucshca.onmicrosoft.com (approval requests, notifications, reports) |
| `Files.ReadWrite.All` | Read and write files in SharePoint document libraries (monthly Excel reports, CSV exports) |

6. After adding all three, click **Grant admin consent for UCSH**
7. Confirm when prompted
8. Verify all three permissions show a green checkmark under "Status" with "Granted for UCSH"

The final permissions table should look like:

| API | Permission | Type | Status |
|-----|-----------|------|--------|
| Microsoft Graph | Sites.ReadWrite.All | Application | Granted for UCSH |
| Microsoft Graph | Mail.Send | Application | Granted for UCSH |
| Microsoft Graph | Files.ReadWrite.All | Application | Granted for UCSH |

---

## Step 4: Restrict Mail.Send Scope (Recommended)

By default, `Mail.Send` allows the app to send email as **any user** in the tenant. To restrict it to only send as `hr@ucshca.onmicrosoft.com`, you can create an **Application Access Policy** using Exchange Online PowerShell.

This step is optional but recommended for security.

### Instructions

1. Open **Exchange Online PowerShell** (or connect via `Connect-ExchangeOnline`)
2. Create a mail-enabled security group (if one doesn't exist) and add `hr@ucshca.onmicrosoft.com` as a member
3. Run the following command (replace `{clientId}` with the Application ID from Step 1):

```powershell
New-ApplicationAccessPolicy -AppId "{clientId}" `
  -PolicyScopeGroupId "hr@ucshca.onmicrosoft.com" `
  -AccessRight RestrictAccess `
  -Description "Restrict UCSH Leave Server to send as HR only"
```

4. Verify the policy:

```powershell
Get-ApplicationAccessPolicy -AppId "{clientId}"
```

> **Note:** Application access policies can take 30–60 minutes to take effect.

---

## Step 5: Verify SharePoint Site Access

No additional SharePoint-specific configuration is needed. The `Sites.ReadWrite.All` permission grants access to all sites in the tenant, including `https://ucshca.sharepoint.com/sites/UCSHBulletinBoard`.

If the organization later wants to restrict access to only the Bulletin Board site (using `Sites.Selected` instead of `Sites.ReadWrite.All`), that is possible but requires additional Graph API calls to grant site-specific permissions. This can be revisited after initial deployment.

---

## Summary of Values to Provide to Development Team

Send these three values securely (password manager, encrypted message, or in-person):

| Value | Example Format |
|-------|---------------|
| Tenant ID | `3f51285e-5594-4af6-869b-fe72b32298fd` |
| Client ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| Client Secret | Long string of characters |

The development team does **not** need admin access to Entra ID. They only need these three values to configure the server.

---

## Ongoing Maintenance

| Task | Frequency | Who |
|------|-----------|-----|
| Rotate client secret before expiry | Every 24 months (or per policy) | Entra ID admin |
| Review API permissions | Annually | Entra ID admin |
| Monitor sign-in logs for the app | As needed | Entra ID admin |

To check the app's sign-in activity: **Entra admin center** → **Identity** → **Applications** → **Enterprise applications** → search for "UCSH Leave Management Server" → **Sign-in logs**.

---

## Questions?

If anything is unclear or if your organization has specific security policies around app registrations (e.g., certificate-based auth instead of client secrets, conditional access policies), please discuss with the development team before proceeding.
