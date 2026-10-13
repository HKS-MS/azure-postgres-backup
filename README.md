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
