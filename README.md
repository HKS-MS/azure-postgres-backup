# PostgreSQL Backup Job

A scheduled-job application that creates a verified custom-format backup of the `salesdb` PostgreSQL database and uploads it to Azure Blob Storage using managed identity.

## Run locally

Requires Python 3.10+, PostgreSQL 16 client tools (`pg_dump` and `pg_restore`), and access to the PostgreSQL server and blob container. Install dependencies from `requirements.txt` and export the environment variables listed in `.env.sample` (`.env` files are not loaded automatically). Set `PGPASSWORD` for local use, or configure a Key Vault URL and secret name. Authenticate to Azure with the Azure CLI or another `DefaultAzureCredential`-supported method.

Run the job with:

```powershell
$env:PGPASSWORD = "your-database-password"
python -m src.backup
```

The process exits with status 0 after a successful upload and nonzero on failure. It writes one object under `salesdb/` named `salesdb_<YYYYMMDD_HHMMSS>.dump`. No password or exception details are written to logs.

## Restore

Download a `.dump` object and restore it into a **separate, empty database** with `pg_restore`, for example:

```powershell
pg_restore --host <server> --username <user> --dbname <separate-database> --no-owner .\salesdb.dump
```

Custom-format database dumps do not include cluster-wide roles or server configuration. Create required roles separately before restoring if ownership or privileges depend on them.

The backup uses `pg_dump`'s consistent single-database snapshot and does not modify the source database. Blob objects include SHA-256, byte count, server, database, timestamp, and PostgreSQL client version metadata.

## Container Image & Public Registry

A pre-built container image is published and maintained on GitHub Container Registry (GHCR):

```text
ghcr.io/hks-ms/azure-postgres-backup:latest
```

This image is publicly accessible and can be pulled directly by Azure Container Apps without configuring registry credentials.

---

## Environment Variables Reference

The backup job container requires the following environment variables:

| Variable | Required | Default | Description | Example |
| :--- | :---: | :---: | :--- | :--- |
| `PGHOST` | **Yes** | — | Fully qualified domain name of the PostgreSQL server | `pgsample89126.postgres.database.azure.com` |
| `PGPORT` | No | `5432` | PostgreSQL server port | `5432` |
| `PGDATABASE` | **Yes** | — | Name of the database to back up (`salesdb`) | `salesdb` |
| `PGUSER` | **Yes** | — | PostgreSQL username or Entra ID Managed Identity name | `id-salesdb-dev-132d` |
| `BLOB_ACCOUNT_URL` | **Yes** | — | Primary blob endpoint URL of the Azure Storage account | `https://mystorageaccount.blob.core.windows.net` |
| `BLOB_CONTAINER` | **Yes** | — | Name of the target Blob Storage container | `postgres-backups` |
| `AZURE_CLIENT_ID` | Conditional | — | Client ID of the User-Assigned Managed Identity (required if using User-Assigned Managed Identity) | `f3a46849-2134-4301-9714-12511fbdd28a` |
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
$PG_SERVER = "pgsample89126"

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
    PGDATABASE="salesdb" `
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

`Dockerfile` packages PostgreSQL 16 and the Python uploader, running as a non-root user. `.dockerignore` permits only the Dockerfile, dependency manifest, and application source into the build context. Local `.env`, credential files, virtual environments and onboarding artifacts are excluded.

The Bicep templates in `infra` target the existing `rg-pg-sample` resource group and do not modify the source PostgreSQL server or firewall. They create an Azure Container Apps scheduled Job with cron `0 2 * * *` (daily 02:00 UTC / 05:00 UTC+3), a one-hour timeout and one retry. Blob lifecycle deletes backups older than 30 days asynchronously. Storage uses LRS.

The architecture uses pure **User-Assigned Managed Identity**:
1. **PostgreSQL Authentication**: In Container Apps, `DefaultAzureCredential` obtains an Entra ID OAuth access token (`https://ossrdbms-aad.database.windows.net/.default`) which is passed as `PGPASSWORD` to `pg_dump`. Zero stored credentials, no Key Vault required.
2. **Blob Storage**: The managed identity is granted `Storage Blob Data Contributor` on the container, enabling passwordless upload via Azure RBAC.
3. **Container Registry**: The managed identity is granted `AcrPull` on the ACR.
4. **Container Apps Environment & Monitoring**: Configured with Log Analytics workspace `log-salesdb-dev-132d`. System events stream into `ContainerAppSystemLogs_CL` and backup execution stdout/stderr streams into `ContainerAppConsoleLogs_CL`. Logs can also be queried via `az containerapp job logs show`.

Deployment is two-phase: create supporting resources with `deployJob=false`, build the image in Azure Container Registry, configure Entra ID authentication for the managed identity on the PostgreSQL server, then redeploy with `deployJob=true` and the actual private ACR image.

Deployment requires explicit approval and verified regional quota. `Microsoft.App` was unregistered during preparation; quota must be rechecked after registration. The existing PostgreSQL firewall permits all Azure services, not only this job; the templates do not widen that rule.

Validate deployment by starting an actual execution, verifying a stored archive and checksum, and restoring into a disposable isolated PostgreSQL 16 instance. `pg_restore --list` is an archive-readability check, not a full restore test. Job failure status and logs are available via `az containerapp job logs show`.

## Manual deployment (PowerShell)

No Azure resources have been provisioned and no image has been built by this project. Run the following steps yourself from this folder. Requires Azure CLI, Bicep (`az bicep install` if missing), and permission to deploy resources and assign roles. Check each command's exit status before continuing. Do not delete the existing resource group: it contains the source PostgreSQL server.

### 1. Select the subscription and check capacity

```powershell
az account set --subscription b2ddaf7a-f872-44d1-957e-6e0554338537
az account show --query "{subscription:name,user:user.name}" -o json

foreach ($provider in @(
  "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.Storage", "Microsoft.ManagedIdentity"
)) {
  az provider register --namespace $provider --wait
  if ($LASTEXITCODE -ne 0) { throw "Provider registration failed: $provider" }
}

az rest --method get --url "https://management.azure.com/subscriptions/b2ddaf7a-f872-44d1-957e-6e0554338537/providers/Microsoft.App/locations/westeurope/usages?api-version=2024-03-01" --query "value[].{name:name.value,used:currentValue,limit:limit}" -o table
```

Check `ManagedEnvironmentCount` and the applicable Consumption core quotas after registration. **Stop if environment capacity is zero/full or cores are insufficient.** Request quota through Azure support/portal rather than silently switching regions.

### 2. Review and deploy supporting resources

The initial deployment does not create a scheduled job. The image reference is non-empty but is not pulled until the second deployment.

```powershell
$image = "crsalesdbdev132d.azurecr.io/postgres-backup:v1"

az deployment sub what-if --location westeurope --template-file .\infra\main.bicep --parameters '@infra/main.parameters.json' deployJob=false containerImage=$image

az deployment sub create --name salesdb-backup-support-132d --location westeurope --template-file .\infra\main.bicep --parameters '@infra/main.parameters.json' deployJob=false containerImage=$image
```

Only proceed if what-if shows the intended new backup resources and the deployment succeeds. The existing PostgreSQL server, database, firewall and resource group are not modified. Blob Storage uses LRS and authorization-protected public endpoints.

### 3. Grant PostgreSQL access to the Managed Identity and build the image

Enable Microsoft Entra ID authentication on Azure Database for PostgreSQL Flexible Server `pgsample89126`:

```powershell
# Get the Managed Identity principal ID and client ID
$identityPrincipalId = az identity show --name id-salesdb-dev-132d --resource-group rg-pg-sample --query principalId -o tsv
$identityClientId = az identity show --name id-salesdb-dev-132d --resource-group rg-pg-sample --query clientId -o tsv

# Add Entra administrator or create database role for the identity:
az postgres flexible-server ad-admin create `
  --resource-group rg-pg-sample `
  --server-name pgsample89126 `
  --display-name "id-salesdb-dev-132d" `
  --object-id $identityPrincipalId `
  --type ServicePrincipal
```

Build the container image using Azure Container Registry:

```powershell
az acr build --registry crsalesdbdev132d --image postgres-backup:v1 --file .\Dockerfile --no-logs .
az acr repository show --name crsalesdbdev132d --image postgres-backup:v1 --query "{digest:digest,created:createdTime}" -o json
```

Wait for ACR build success. For immutable deployments, set `$image` to `crsalesdbdev132d.azurecr.io/postgres-backup@sha256:<digest>` using the returned digest.

### 4. Enable the schedule and run a backup now

```powershell
az deployment sub what-if --location westeurope --template-file .\infra\main.bicep --parameters '@infra/main.parameters.json' deployJob=true containerImage=$image

az deployment sub create --name salesdb-backup-job-132d --location westeurope --template-file .\infra\main.bicep --parameters '@infra/main.parameters.json' deployJob=true containerImage=$image

az containerapp job start --name job-salesdb-dev-132d --resource-group rg-pg-sample
az containerapp job execution list --name job-salesdb-dev-132d --resource-group rg-pg-sample --query "[].{name:name,status:properties.status,start:properties.startTime,end:properties.endTime}" -o table
```

A successful start request is not a successful backup. Wait for execution status `Succeeded`. You can inspect container logs directly via Azure CLI or Log Analytics:

```powershell
# Option A: Stream logs directly via Container Apps CLI
az containerapp job logs show --name job-salesdb-dev-132d --resource-group rg-pg-sample --follow

# Option B: Query Log Analytics workspace
$wsId = az monitor log-analytics workspace show -g rg-pg-sample -n log-salesdb-dev-132d --query customerId -o tsv
az monitor log-analytics query -w $wsId --analytics-query "ContainerAppConsoleLogs_CL | where ContainerJobName_s == 'job-salesdb-dev-132d' | project TimeGenerated, Log_s | order by TimeGenerated desc" -t PT1H -o table
```

### 5. Verify the stored archive and restore

In storage account `stsalesdbdev132d`, verify a new object under `postgres-backups/salesdb/`. Check metadata `database`, `server`, `bytes` and `sha256`. A human operator needs container-scoped **Storage Blob Data Reader** to list/download through Entra authentication; only the job identity is granted blob access by these templates. Request that scoped operator role if needed, not a storage key.

With that role, list and download the actual blob:

```powershell
az storage blob list --account-name stsalesdbdev132d --container-name postgres-backups --auth-mode login --query "[].{name:name,bytes:properties.contentLength,metadata:metadata}" -o json
# Replace the blob name below with the exact returned path.
$blobName = "salesdb/salesdb_YYYYMMDD_HHMMSS.dump"
az storage blob download --account-name stsalesdbdev132d --container-name postgres-backups --name $blobName --file .\salesdb.dump --auth-mode login --overwrite false
(Get-FileHash .\salesdb.dump -Algorithm SHA256).Hash.ToLowerInvariant()
```

Compare the local SHA256 to blob metadata and reject any mismatch. Use PostgreSQL 16 `pg_restore --list` to inspect the archive, then `pg_restore --exit-on-error --no-owner --no-privileges --dbname <disposable-database> .\salesdb.dump` into a **separate isolated PostgreSQL 16 instance** and inspect restored tables/row counts. Never restore into `salesdb` on the source server. For a disposable Docker instance, use a separately installed Docker runtime on your workstation; Docker is not required for the Azure image build.

The scheduled job will continue running at 02:00 UTC. Lifecycle cleanup may run after the 30-day threshold, so expired blobs can remain briefly. Configure failure notifications and recurring restore drills separately before relying on unattended production backups.
