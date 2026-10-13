targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string
@minLength(1)
param location string
param sessionId string
param deployedBy string
param createdAt string
param deployJob bool = false
@minLength(1)
param containerImage string = 'crsalesdbdev132d.azurecr.io/postgres-backup:v2'

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
  SecurityControl: 'Ignore'
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' existing = {
  name: 'rg-pg-sample'
}

module identity './modules/managed-identity.bicep' = {
  name: 'managed-identity'
  scope: rg
  params: {
    identityName: 'id-salesdb-dev-132d'
    location: location
    tags: tags
  }
}

module acr './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    registryName: 'crsalesdbdev132d'
    location: location
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    storageName: 'stsalesdbdev132d'
    location: location
    tags: tags
  }
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: {
    workspaceName: 'log-salesdb-dev-132d'
    location: location
    tags: tags
  }
}

module environment './modules/container-apps-environment.bicep' = {
  name: 'container-apps-environment'
  scope: rg
  dependsOn: [
    logAnalytics
  ]
  params: {
    environmentName: 'cae-salesdb-dev-132d'
    location: location
    tags: tags
    workspaceName: 'log-salesdb-dev-132d'
  }
}

module job './modules/container-apps-job.bicep' = {
  name: 'container-apps-job'
  scope: rg
  dependsOn: [
    roleAssignments
  ]
  params: {
    jobName: 'job-salesdb-dev-132d'
    location: location
    tags: tags
    managedEnvironmentId: environment.outputs.id
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    acrLoginServer: acr.outputs.loginServer
    containerImage: containerImage
    deployJob: deployJob
    postgresHost: 'pgsample89126.postgres.database.azure.com'
    postgresUser: 'id-salesdb-dev-132d'
    storageAccountUrl: storage.outputs.blobEndpoint
    storageContainer: 'postgres-backups'
  }
}

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  dependsOn: [
    acr
    storage
  ]
  params: {
    identityPrincipalId: identity.outputs.principalId
    registryName: 'crsalesdbdev132d'
    storageName: 'stsalesdbdev132d'
    storageContainerName: 'postgres-backups'
  }
}
