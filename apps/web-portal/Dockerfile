# Web portal: multi-stage build (node build → nginx static serve).
#   docker build -f apps/web-portal/Dockerfile -t ai-employee/web-portal .

# ---- stage 1: build the Vite SPA ----
FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . ./
RUN npm run build

# ---- stage 2: serve the static bundle with nginx ----
FROM nginx:1.27-alpine AS runtime
# SPA fallback + API proxy to the in-cluster backend services.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
