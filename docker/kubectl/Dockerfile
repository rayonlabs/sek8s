ARG KUBECTL_VERSION=1.35
FROM bitnami/kubectl:${KUBECTL_VERSION} AS base

##################################################
## PRODUCTION ##
##################################################

FROM base AS production

##################################################
## DEVELOPMENT ##
##################################################

FROM build AS development