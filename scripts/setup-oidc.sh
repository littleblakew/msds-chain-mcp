#!/usr/bin/env bash
# Add OIDC federated credential for msds-chain-mcp repo CI/CD
#
# This allows GitHub Actions in littleblakew/msds-chain-mcp to authenticate
# to Azure without secrets, using the same service principal as msds-chain.
#
# Prerequisites:
#   - az CLI logged in with sufficient permissions
#   - The App Registration used by msds-chain CI/CD already exists
#
# Usage:
#   ./scripts/setup-oidc.sh

set -euo pipefail

# Same service principal as msds-chain (reuse existing OIDC setup)
# Get the App Object ID from Azure Portal > App Registrations > msds-chain-cicd
APP_OBJECT_ID=$(az ad app list --display-name "msds-chain-cicd" --query "[0].id" -o tsv)

if [ -z "$APP_OBJECT_ID" ]; then
  echo "Error: Could not find app registration 'msds-chain-cicd'"
  echo "Check the display name in Azure Portal > App Registrations"
  exit 1
fi

echo "App Object ID: ${APP_OBJECT_ID}"
echo ""

# Add federated credential for msds-chain-mcp repo (main branch)
echo "Adding OIDC credential for littleblakew/msds-chain-mcp:main..."
az ad app federated-credential create \
  --id "${APP_OBJECT_ID}" \
  --parameters '{
    "name": "msds-chain-mcp-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:littleblakew/msds-chain-mcp:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"],
    "description": "GitHub Actions OIDC for msds-chain-mcp repo (main branch)"
  }'

echo ""

# Add federated credential for environment
echo "Adding OIDC credential for littleblakew/msds-chain-mcp:environment:production..."
az ad app federated-credential create \
  --id "${APP_OBJECT_ID}" \
  --parameters '{
    "name": "msds-chain-mcp-env-production",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:littleblakew/msds-chain-mcp:environment:production",
    "audiences": ["api://AzureADTokenExchange"],
    "description": "GitHub Actions OIDC for msds-chain-mcp repo (production environment)"
  }'

echo ""
echo "=== Done ==="
echo ""
echo "Next: Add these secrets to the msds-chain-mcp repo:"
echo "  gh secret set AZURE_CLIENT_ID     --repo littleblakew/msds-chain-mcp --body <client-id>"
echo "  gh secret set AZURE_TENANT_ID     --repo littleblakew/msds-chain-mcp --body <tenant-id>"
echo "  gh secret set AZURE_SUBSCRIPTION_ID --repo littleblakew/msds-chain-mcp --body <subscription-id>"
echo ""
echo "These values are the same as in the msds-chain repo secrets."
