param jobName string
param location string
param tags object
param managedEnvironmentId string
param identityId string
param identityClientId string
param acrLoginServer string
@minLength(1)
param containerImage string
param deployJob bool
param postgresHost string
param postgresUser string
param databaseName string = 'mydb'
param storageAccountUrl string
param storageContainer string

resource job 'Microsoft.App/jobs@2026-07-01' = if (deployJob) {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: managedEnvironmentId
    configuration: {
      triggerType: 'Schedule'
      replicaRetryLimit: 1
      replicaTimeout: 3600
      scheduleTriggerConfig: {
        cronExpression: '0 2 * * *'
        replicaCompletionCount: 1
        parallelism: 1
      }
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'postgres-backup'
          image: containerImage
          command: [
            'python3'
            '-m'
            'src.backup'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'PGHOST', value: postgresHost }
            { name: 'PGPORT', value: '5432' }
            { name: 'PGDATABASE', value: databaseName }
            { name: 'PGUSER', value: postgresUser }
            { name: 'PGSSLMODE', value: 'verify-full' }
            { name: 'PGSSLROOTCERT', value: 'system' }
            { name: 'BLOB_ACCOUNT_URL', value: storageAccountUrl }
            { name: 'BLOB_CONTAINER', value: storageContainer }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
          ]
        }
      ]
    }
  }
}

output id string = deployJob ? job.id : ''
