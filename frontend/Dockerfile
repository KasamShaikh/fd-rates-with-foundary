# ---------------------------------------------------------------------------
# FD Rate Aggregator frontend container
#
# Stage 1 builds the React SPA. Stage 2 serves static files with Nginx and
# proxies /api to the in-cluster backend service.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm install --no-audit --no-fund

COPY . .

# Empty default keeps same-origin API calls (frontend host + /api/...)
ARG REACT_APP_API_BASE_URL=
ENV REACT_APP_API_BASE_URL=${REACT_APP_API_BASE_URL}

# Windows-built source loses POSIX exec bits, so invoke the bundler via node
# instead of relying on the react-scripts shebang.
RUN node node_modules/react-scripts/bin/react-scripts.js build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
