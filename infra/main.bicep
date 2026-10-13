targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string = 'pg-backup'
@minLength(1)
param location string = 'westeurope'
param resourceGroupName string = 'rg-postgres-backup'
param postgresHost string = 'my-postgres-server.postgres.database.azure.com'
param databaseName string = 'mydb'
param deployJob bool = false
@minLength(1)
param containerImage string = 'ghcr.io/hks-ms/azure-postgres-backup:latest'

var uniqueSuffix = uniqueString(subscription().id, environmentName, location)
var managedIdentityName = 'id-${environmentName}'
var acrName = 'cr${take(replace(environmentName, '-', ''), 14)}${take(uniqueSuffix, 6)}'
var storageAccountName = 'st${take(replace(environmentName, '-', ''), 14)}${take(uniqueSuffix, 6)}'
var logAnalyticsName = 'log-${environmentName}'
var caeName = 'cae-${environmentName}'
var jobName = 'job-${environmentName}'

var tags = {
  environment: environmentName
  workload: 'postgres-backup'
  SecurityControl: 'Ignore'
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' existing = {
  name: resourceGroupName
}

module identity './modules/managed-identity.bicep' = {
  name: 'managed-identity'
  scope: rg
  params: {
    identityName: managedIdentityName
    location: location
    tags: tags
  }
}

module acr './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    registryName: acrName
    location: location
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    storageName: storageAccountName
    location: location
    tags: tags
  }
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: {
    workspaceName: logAnalyticsName
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
    environmentName: caeName
    location: location
    tags: tags
    workspaceName: logAnalyticsName
  }
}

module job './modules/container-apps-job.bicep' = {
  name: 'container-apps-job'
  scope: rg
  dependsOn: [
    roleAssignments
  ]
  params: {
    jobName: jobName
    location: location
    tags: tags
    managedEnvironmentId: environment.outputs.id
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    acrLoginServer: acr.outputs.loginServer
    containerImage: containerImage
    deployJob: deployJob
    postgresHost: postgresHost
    postgresUser: managedIdentityName
    databaseName: databaseName
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
    registryName: acrName
    storageName: storageAccountName
    storageContainerName: 'postgres-backups'
  }
}
