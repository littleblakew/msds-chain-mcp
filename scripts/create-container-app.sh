#!/usr/bin/env bash
# Create the Azure Container App for MSDS Chain MCP Server
#
# Prerequisites:
#   - az CLI logged in
#   - ACR 'acrmsdschain' exists
#   - Container Apps Environment exists in rg-msds-chain-dev
#
# Usage:
#   ./scripts/create-container-app.sh
#
# After running:
#   1. Note the FQDN printed at the end
#   2. Ask Kenny to add DNS CNAME: mcp.lagentbot.com → <FQDN>
#   3. Bind custom domain + managed certificate

set -euo pipefail

# --- Config ---
RESOURCE_GROUP="rg-msds-chain-dev"
APP_NAME="msds-chain-mcp"
ACR_SERVER="acrmsdschain.azurecr.io"
IMAGE="${ACR_SERVER}/msds-chain-mcp:latest"
ENVIRONMENT="cae-msds-chain-dev"  # reuse existing env
LOCATION="southeastasia"

echo "=== Step 1: Build & push Docker image ==="
az acr build \
  --registry acrmsdschain \
  --image msds-chain-mcp:latest \
  --file Dockerfile \
  .

echo "=== Step 2: Create Container App ==="
az containerapp create \
  --name "${APP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --environment "${ENVIRONMENT}" \
  --image "${IMAGE}" \
  --target-port 8080 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 3 \
  --cpu 0.25 \
  --memory 0.5Gi \
  --env-vars \
    MSDS_API_KEY=secretref:msds-api-key \
    MSDS_MCP_HOST=0.0.0.0 \
    MSDS_MCP_PORT=8080 \
    MSDS_MCP_TRANSPORT=streamable-http \
    MSDS_OAUTH_ENABLED=1 \
    MSDS_OAUTH_ISSUER=https://mcp.lagentbot.com \
    MSDS_OAUTH_SECRET=secretref:oauth-secret \
  --registry-server "${ACR_SERVER}"

echo ""
echo "=== Step 3: Set secrets ==="
echo "Run these commands to set the secrets:"
echo ""
echo "  az containerapp secret set \\"
echo "    --name ${APP_NAME} \\"
echo "    --resource-group ${RESOURCE_GROUP} \\"
echo "    --secrets msds-api-key=<your-prod-api-key> oauth-secret=<random-32-hex>"
echo ""
echo "  # Generate oauth-secret with: python -c 'import secrets; print(secrets.token_hex(32))'"
echo ""

echo "=== Step 4: Get FQDN ==="
FQDN=$(az containerapp show \
  --name "${APP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --query "properties.configuration.ingress.fqdn" \
  --output tsv)

echo ""
echo "Container App FQDN: ${FQDN}"
echo ""
echo "=== Next steps ==="
echo "1. Ask Kenny to add DNS CNAME:"
echo "   mcp.lagentbot.com  →  ${FQDN}"
echo ""
echo "2. After DNS propagates, bind custom domain:"
echo "   az containerapp hostname add \\"
echo "     --name ${APP_NAME} \\"
echo "     --resource-group ${RESOURCE_GROUP} \\"
echo "     --hostname mcp.lagentbot.com"
echo ""
echo "   az containerapp hostname bind \\"
echo "     --name ${APP_NAME} \\"
echo "     --resource-group ${RESOURCE_GROUP} \\"
echo "     --hostname mcp.lagentbot.com \\"
echo "     --environment ${ENVIRONMENT} \\"
echo "     --validation-method CNAME"
echo ""
echo "3. Verify:"
echo "   curl https://mcp.lagentbot.com/health"
echo "   curl https://mcp.lagentbot.com/.well-known/oauth-authorization-server"
