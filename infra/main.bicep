// SDS POC infrastructure: Storage (Blob/Queue/Table), Flex Consumption Python Function App,
// Managed Identity RBAC, Key Vault (OpenAI key), Application Insights, CORS, Easy Auth.
// Validated with `az bicep build` only. NOT deployed by the POC; deployment is a manual, reviewed step.
targetScope = 'resourceGroup'

@description('Short base name used to derive resource names (lowercase letters/digits).')
@minLength(3)
@maxLength(16)
param baseName string

@description('Region that supports Flex Consumption (check: az functionapp list-flexconsumption-locations).')
param location string = resourceGroup().location

@description('Maximum on-demand instances per function group. Docs: lowest allowed value is 1, highest 1000. Keep small for the POC so parse concurrency stays bounded (host.json batchSize=1).')
@minValue(1)
@maxValue(1000)
param maximumInstanceCount int = 1

@allowed([512, 2048, 4096])
param instanceMemoryMB int = 2048

@description('Allowed browser origins (Angular static hosting URL(s)).')
param corsAllowedOrigins array = ['http://localhost:4200']

@description('mock | openai. With openai, the key comes from Key Vault and both model names are required.')
@allowed(['mock', 'openai'])
param extractionProvider string = 'mock'

@secure()
@description('OpenAI API key; stored in Key Vault and referenced from app settings. Leave empty for mock.')
param openAiApiKey string = ''

param openAiModelBaseline string = ''
param openAiModelCandidate string = ''
param openAiTimeoutSeconds string = '180'
param openAiMaxAttempts string = '2'
param openAiFileDetail string = ''

@description('Entra ID application (client) ID used by App Service Authentication. Required for authMode=easyauth.')
param entraClientId string = ''
param entraTenantId string = tenant().tenantId

@description('Allowed audiences for incoming tokens (e.g. api://<clientId>).')
param entraAllowedAudiences array = []

var suffix = toLower(uniqueString(resourceGroup().id, baseName))
var storageName = take('st${replace(baseName, '-', '')}${suffix}', 24)
var functionAppName = 'func-${baseName}-${suffix}'
var planName = 'plan-${baseName}-${suffix}'
var kvName = take('kv-${baseName}-${suffix}', 24)
var deploymentContainer = 'app-package'
var sdsContainer = 'sds'
var queueName = 'sds-jobs'

// ---------------------------------------------------------------- Storage
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // identity-based access only (host, app, deployment)
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: true, days: 7 } }
}

resource deployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainer
}

resource dataContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: sdsContainer
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource jobsQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: queueName
}

resource poisonQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: '${queueName}-poison'
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

// ---------------------------------------------------------------- Monitoring
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${baseName}-${suffix}'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${baseName}-${suffix}'
  location: location
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logAnalytics.id }
}

// ---------------------------------------------------------------- Key Vault
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    publicNetworkAccess: 'Enabled'
  }
}

resource openAiSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(openAiApiKey)) {
  parent: keyVault
  name: 'openai-api-key'
  properties: { value: openAiApiKey }
}

// ---------------------------------------------------------------- Function App (Flex Consumption)
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

var baseAppSettings = [
  { name: 'AzureWebJobsStorage__accountName', value: storage.name }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
  { name: 'EXTRACTION_PROVIDER', value: extractionProvider }
  { name: 'OPENAI_MODEL_BASELINE', value: openAiModelBaseline }
  { name: 'OPENAI_MODEL_CANDIDATE', value: openAiModelCandidate }
  { name: 'OPENAI_TIMEOUT_SECONDS', value: openAiTimeoutSeconds }
  { name: 'OPENAI_MAX_ATTEMPTS', value: openAiMaxAttempts }
  { name: 'OPENAI_FILE_DETAIL', value: openAiFileDetail }
  { name: 'MAX_UPLOAD_BYTES', value: '20971520' }
  { name: 'PROCESSING_LEASE_SECONDS', value: '600' }
  { name: 'SDS_BLOB_CONTAINER', value: sdsContainer }
  { name: 'SDS_QUEUE_NAME', value: queueName }
  { name: 'AUTH_MODE', value: 'easyauth' } // local_dev is refused on Azure by the code as well
  { name: 'PYTHON_ENABLE_WORKER_EXTENSIONS', value: '0' }
]
var openAiKeySetting = empty(openAiApiKey) ? [] : [
  { name: 'OPENAI_API_KEY', value: '@Microsoft.KeyVault(SecretUri=${keyVault.properties.vaultUri}secrets/openai-api-key)' }
]

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: { name: 'python', version: '3.12' }
    }
    siteConfig: {
      cors: { allowedOrigins: corsAllowedOrigins, supportCredentials: false }
      appSettings: concat(baseAppSettings, openAiKeySetting)
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
    }
  }
  dependsOn: [deployContainer, dataContainer, jobsQueue, tableService]
}

// App Service Authentication (Microsoft Entra ID). Browser must send a bearer token for the registered API.
resource authSettings 'Microsoft.Web/sites/config@2024-04-01' = if (!empty(entraClientId)) {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: { requireAuthentication: true, unauthenticatedClientAction: 'Return401', excludedPaths: ['/api/health'] }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: { clientId: entraClientId, openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0' }
        validation: { allowedAudiences: entraAllowedAudiences }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

// ---------------------------------------------------------------- RBAC for the app identity
var roleIds = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

resource blobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, roleIds.storageBlobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageBlobDataOwner)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, roleIds.storageQueueDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageQueueDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, roleIds.storageTableDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.storageTableDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, roleIds.keyVaultSecretsUser)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.keyVaultSecretsUser)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, functionApp.id, roleIds.monitoringMetricsPublisher)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.monitoringMetricsPublisher)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output storageAccountName string = storage.name
output keyVaultName string = keyVault.name
output appInsightsName string = appInsights.name
