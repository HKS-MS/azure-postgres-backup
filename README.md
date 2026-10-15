# PostgreSQL Backup Job

A scheduled-job application that creates a verified custom-format backup of a PostgreSQL database and uploads it to Azure Blob Storage using managed identity.

## Run locally

Requires Python 3.10+, PostgreSQL 16 or 18 client tools (`pg_dump` and `pg_restore`), and access to the PostgreSQL server and blob container. Use PostgreSQL 18 tools for PostgreSQL 18 servers. Install dependencies from `requirements.txt` and export the environment variables listed in `.env.sample` (`.env` files are not loaded automatically). Set `PGPASSWORD` for local use, or leave it unset for managed identity authentication. Authenticate to Azure with the Azure CLI or another `DefaultAzureCredential`-supported method.

Run the job with:

```powershell
$env:PGPASSWORD = "your-database-password"
python -m src.backup
```

The process exits with status 0 after a successful upload and nonzero on failure. It writes one object under `<database>/` named `<database>_<YYYYMMDD_HHMMSS>.dump`. No password or exception details are written to logs.

## Restore

Download a `.dump` object and restore it into a **separate, empty database** with `pg_restore`, for example:

```powershell
pg_restore --host <server> --username <user> --dbname <separate-database> --no-owner .\mydb.dump
```

Custom-format database dumps do not include cluster-wide roles or server configuration. Create required roles separately before restoring if ownership or privileges depend on them.

The backup uses `pg_dump`'s consistent single-database snapshot and does not modify the source database. Blob objects include SHA-256, byte count, server, database, timestamp, and PostgreSQL client version metadata.

## Container Image & Public Registry

A pre-built container image is published and maintained on GitHub Container Registry (GHCR):

```text
ghcr.io/hks-ms/azure-postgres-backup:latest
```

This image is publicly accessible and can be pulled directly by Azure Container Apps without configuring registry credentials.

For **PostgreSQL 18**, use the separate image:

```text
ghcr.io/hks-ms/azure-postgres-backup:pg18
```

`latest` and existing SHA tags retain PostgreSQL 16 clients. The `pg18` image
contains PostgreSQL 18 `pg_dump` and `pg_restore`; use PostgreSQL 18 restore tools
and a PostgreSQL 18 or newer destination for its archives. Environment variables
and managed identity configuration are unchanged. In the Azure portal, edit the
Container Apps Job's container image to the `pg18` reference and save; this does
not require changing its schedule or environment variables.

To publish only `pg18`, manually run **Build and Publish Docker Image** in GitHub
Actions with `postgres_major` set to `18`. This runs unit tests and an isolated
PostgreSQL 18 dump/restore check before publication, without updating `latest`
or PostgreSQL 16 SHA tags. Local builds default to PostgreSQL 16; select 18 with
`docker build --build-arg POSTGRES_MAJOR=18 -t postgres-backup:pg18 .`.

---

## Environment Variables Reference

The backup job container requires the following environment variables:

| Variable | Required | Default | Description | Example |
| :--- | :---: | :---: | :--- | :--- |
| `PGHOST` | **Yes** | — | Fully qualified domain name of the PostgreSQL server | `my-postgres-server.postgres.database.azure.com` |
| `PGPORT` | No | `5432` | PostgreSQL server port | `5432` |
| `PGDATABASE` | **Yes** | — | Name of the database to back up | `mydb` |
| `PGUSER` | **Yes** | — | PostgreSQL username or Entra ID Managed Identity name | `id-postgres-backup` |
| `BLOB_ACCOUNT_URL` | **Yes** | — | Primary blob endpoint URL of the Azure Storage account | `https://mystorageaccount.blob.core.windows.net` |
| `BLOB_CONTAINER` | **Yes** | — | Name of the target Blob Storage container | `postgres-backups` |
| `AZURE_CLIENT_ID` | Conditional | — | Client ID of the User-Assigned Managed Identity (required if using User-Assigned Managed Identity) | `00000000-0000-0000-0000-000000000000` |
| `PGPASSWORD` | No | — | Database password. **Leave unset in Azure** to automatically use passwordless Microsoft Entra ID authentication via Managed Identity | `my-secret-password` (local dev only) |
| `PGSSLMODE` | No | `verify-full` | SSL verification mode. Must be `verify-full` for secure connections | `verify-full` |
| `PGSSLROOTCERT` | No | `system` | Path to SSL root CA certificates or `system` | `system` |

---

## Deploying to Azure Container Apps (Step-by-Step)

Follow these steps to deploy and run the backup image in any Azure subscription using Azure CLI.

### Prerequisites

- Azure CLI (`az`) installed and authenticated (`az login`)
- An existing Azure PostgreSQL Flexible Server with Microsoft Entra ID authentication enabled
- An Azure Storage Account with a blob container for backups

### Step 1: Create Resource Group & Managed Identity

```powershell
$RESOURCE_GROUP = "rg-postgres-backup"
$LOCATION = "westeurope"
$IDENTITY_NAME = "id-postgres-backup"

# Create Resource Group
az group create --name $RESOURCE_GROUP --location $LOCATION

# Create User-Assigned Managed Identity
az identity create --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP

# Retrieve Identity IDs
$IDENTITY_ID = az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query id -o tsv
$CLIENT_ID = az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query clientId -o tsv
$PRINCIPAL_ID = az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query principalId -o tsv
```

### Step 2: Grant Permissions to Managed Identity

#### A. Grant Storage Blob Data Contributor on Azure Storage
```powershell
$STORAGE_ACCOUNT = "mystorageaccount"
$STORAGE_ID = az storage account show --name $STORAGE_ACCOUNT --query id -o tsv

az role assignment create `
  --assignee-object-id $PRINCIPAL_ID `
  --assignee-principal-type ServicePrincipal `
  --role "Storage Blob Data Contributor" `
  --scope $STORAGE_ID
```

#### B. Grant Entra ID Access on Azure PostgreSQL Flexible Server
```powershell
$PG_SERVER = "my-postgres-server"

az postgres flexible-server ad-admin create `
  --resource-group $RESOURCE_GROUP `
  --server-name $PG_SERVER `
  --display-name $IDENTITY_NAME `
  --object-id $PRINCIPAL_ID `
  --type ServicePrincipal
```

### Step 3: Create Log Analytics Workspace & Container Apps Environment

```powershell
$WORKSPACE_NAME = "log-postgres-backup"
$ENVIRONMENT_NAME = "cae-postgres-backup"

# Create Log Analytics Workspace
az monitor log-analytics workspace create `
  --resource-group $RESOURCE_GROUP `
  --workspace-name $WORKSPACE_NAME `
  --location $LOCATION

$WORKSPACE_CUSTOMER_ID = az monitor log-analytics workspace show `
  --resource-group $RESOURCE_GROUP `
  --workspace-name $WORKSPACE_NAME `
  --query customerId -o tsv

$WORKSPACE_KEY = az monitor log-analytics workspace get-shared-keys `
  --resource-group $RESOURCE_GROUP `
  --workspace-name $WORKSPACE_NAME `
  --query primarySharedKey -o tsv

# Create Container Apps Managed Environment
az containerapp env create `
  --name $ENVIRONMENT_NAME `
  --resource-group $RESOURCE_GROUP `
  --location $LOCATION `
  --logs-workspace-id $WORKSPACE_CUSTOMER_ID `
  --logs-workspace-key $WORKSPACE_KEY
```

### Step 4: Create Scheduled Container Apps Job

Run the public GHCR image on a daily schedule (e.g. `0 2 * * *` for 02:00 UTC):

```powershell
$JOB_NAME = "job-postgres-backup"
$IMAGE = "ghcr.io/hks-ms/azure-postgres-backup:latest"
$PG_HOST = "$PG_SERVER.postgres.database.azure.com"
$BLOB_URL = "https://$STORAGE_ACCOUNT.blob.core.windows.net"

az containerapp job create `
  --name $JOB_NAME `
  --resource-group $RESOURCE_GROUP `
  --environment $ENVIRONMENT_NAME `
  --trigger-type Schedule `
  --cron-expression "0 2 * * *" `
  --replica-timeout 3600 `
  --replica-retry-limit 1 `
  --image $IMAGE `
  --cpu 0.5 `
  --memory 1.0Gi `
  --user-assigned $IDENTITY_ID `
  --command "python3" "-m" "src.backup" `
  --env-vars `
    PGHOST=$PG_HOST `
    PGPORT="5432" `
    PGDATABASE="mydb" `
    PGUSER=$IDENTITY_NAME `
    PGSSLMODE="verify-full" `
    PGSSLROOTCERT="system" `
    BLOB_ACCOUNT_URL=$BLOB_URL `
    BLOB_CONTAINER="postgres-backups" `
    AZURE_CLIENT_ID=$CLIENT_ID
```

### Step 5: Test Execution and View Logs

Trigger a manual execution immediately:

```powershell
# Start Job Execution
az containerapp job start --name $JOB_NAME --resource-group $RESOURCE_GROUP

# List executions and check status
az containerapp job execution list --name $JOB_NAME --resource-group $RESOURCE_GROUP -o table

# Stream live container console logs
az containerapp job logs show --name $JOB_NAME --resource-group $RESOURCE_GROUP --follow
```

Or query the Log Analytics workspace:

```powershell
az monitor log-analytics query `
  -w $WORKSPACE_CUSTOMER_ID `
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerJobName_s == '$JOB_NAME' | project TimeGenerated, Log_s | order by TimeGenerated desc" `
  -t PT1H -o table
```

---

## Container and Azure schedule

`Dockerfile` packages PostgreSQL 16 and the Python uploader, running as a non-root user. `.dockerignore` permits only the Dockerfile, dependency manifest, and application source into the build context. Local `.env`, credential files, and virtual environments are excluded.

The Bicep templates in `infra` deploy to an Azure resource group and create an Azure Container Apps scheduled Job with cron `0 2 * * *` (daily 02:00 UTC), a one-hour timeout, and one retry. Blob lifecycle deletes backups older than 30 days asynchronously. Storage uses LRS.

The architecture uses pure **User-Assigned Managed Identity**:
1. **PostgreSQL Authentication**: In Container Apps, `DefaultAzureCredential` obtains an Entra ID OAuth access token (`https://ossrdbms-aad.database.windows.net/.default`) which is passed as `PGPASSWORD` to `pg_dump`. Zero stored credentials, no Key Vault required.
2. **Blob Storage**: The managed identity is granted `Storage Blob Data Contributor` on the container, enabling passwordless upload via Azure RBAC.
3. **Container Registry**: The managed identity is granted `AcrPull` on the ACR (or pulls freely from the public GHCR image).
4. **Container Apps Environment & Monitoring**: Configured with Log Analytics workspace. System events stream into `ContainerAppSystemLogs_CL` and backup execution stdout/stderr streams into `ContainerAppConsoleLogs_CL`. Logs can also be queried via `az containerapp job logs show`.

## Deploying via Bicep (PowerShell)

Run the following steps to deploy the complete infrastructure via Bicep. Check each command's exit status before continuing.

### 1. Select the subscription and check capacity

```powershell
$SUBSCRIPTION_ID = "<your-subscription-id>"
$LOCATION = "westeurope"

az account set --subscription $SUBSCRIPTION_ID

foreach ($provider in @(
  "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.Storage", "Microsoft.ManagedIdentity", "Microsoft.OperationalInsights"
)) {
  az provider register --namespace $provider --wait
}
```

### 2. Review and deploy infrastructure

```powershell
$RESOURCE_GROUP = "rg-postgres-backup"
az group create --name $RESOURCE_GROUP --location $LOCATION

az deployment sub what-if `
  --location $LOCATION `
  --template-file .\infra\main.bicep `
  --parameters '@infra/main.parameters.json' `
  deployJob=false

az deployment sub create `
  --name "postgres-backup-support" `
  --location $LOCATION `
  --template-file .\infra\main.bicep `
  --parameters '@infra/main.parameters.json' `
  deployJob=false
```

### 3. Grant PostgreSQL access to the Managed Identity

Enable Microsoft Entra ID authentication on Azure Database for PostgreSQL Flexible Server:

```powershell
$IDENTITY_NAME = "id-pg-backup"
$PG_SERVER = "my-postgres-server"

# Get the Managed Identity principal ID
$identityPrincipalId = az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query principalId -o tsv

# Add Entra administrator for the identity:
az postgres flexible-server ad-admin create `
  --resource-group $RESOURCE_GROUP `
  --server-name $PG_SERVER `
  --display-name $IDENTITY_NAME `
  --object-id $identityPrincipalId `
  --type ServicePrincipal
```

### 4. Enable the schedule and run a backup

```powershell
az deployment sub create `
  --name "postgres-backup-job" `
  --location $LOCATION `
  --template-file .\infra\main.bicep `
  --parameters '@infra/main.parameters.json' `
  deployJob=true

$JOB_NAME = "job-pg-backup"
az containerapp job start --name $JOB_NAME --resource-group $RESOURCE_GROUP
az containerapp job execution list --name $JOB_NAME --resource-group $RESOURCE_GROUP --query "[].{name:name,status:properties.status,start:properties.startTime,end:properties.endTime}" -o table
```

Wait for execution status `Succeeded`. You can inspect container logs directly via Azure CLI or Log Analytics:

```powershell
# Option A: Stream logs directly via Container Apps CLI
az containerapp job logs show --name $JOB_NAME --resource-group $RESOURCE_GROUP --follow

# Option B: Query Log Analytics workspace
$wsId = az monitor log-analytics workspace show -g $RESOURCE_GROUP -n "log-pg-backup" --query customerId -o tsv
az monitor log-analytics query -w $wsId --analytics-query "ContainerAppConsoleLogs_CL | where ContainerJobName_s == '$JOB_NAME' | project TimeGenerated, Log_s | order by TimeGenerated desc" -t PT1H -o table
```

### 5. Verify the stored archive and restore

In your storage account, verify the new backup object under `postgres-backups/<database-name>/`. Check metadata `database`, `server`, `bytes` and `sha256`.

```powershell
$STORAGE_ACCOUNT = "mystorageaccount"
az storage blob list --account-name $STORAGE_ACCOUNT --container-name postgres-backups --auth-mode login --query "[].{name:name,bytes:properties.contentLength,metadata:metadata}" -o json

# Download and verify hash
$blobName = "<database-name>/<database-name>_YYYYMMDD_HHMMSS.dump"
az storage blob download --account-name $STORAGE_ACCOUNT --container-name postgres-backups --name $blobName --file .\database.dump --auth-mode login --overwrite false
(Get-FileHash .\database.dump -Algorithm SHA256).Hash.ToLowerInvariant()
```

Restore into a **separate isolated PostgreSQL 16 instance**:

```powershell
pg_restore --exit-on-error --no-owner --no-privileges --dbname <disposable-database> .\database.dump
```
