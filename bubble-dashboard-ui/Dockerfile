# ---- Stage 1: Build ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
# npm ci быстрее npm install и гарантирует воспроизводимость
RUN npm ci
COPY . .
RUN npm run build

# ---- Stage 2: Production (Nginx) ----
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html

# Nginx конфиг:
# - SPA: все маршруты → index.html
# - /api/ → проксируем на bubble-api контейнер (Docker внутренняя сеть)
# Это решает проблему: браузерный localhost:8000 не резолвится внутри Docker
RUN printf 'server {\n\
    listen 80;\n\
    server_name localhost;\n\
\n\
    # Раздача статики React приложения\n\
    location / {\n\
        root   /usr/share/nginx/html;\n\
        index  index.html;\n\
        try_files $uri $uri/ /index.html;\n\
    }\n\
\n\
    # Проксирование API запросов на FastAPI контейнер (имя сервиса из docker-compose)\n\
    location /api/ {\n\
        proxy_pass http://api:8000/api/;\n\
        proxy_set_header Host $host;\n\
        proxy_set_header X-Real-IP $remote_addr;\n\
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n\
        proxy_read_timeout 60s;\n\
    }\n\
\n\
    error_page 500 502 503 504 /50x.html;\n\
    location = /50x.html {\n\
        root /usr/share/nginx/html;\n\
    }\n\
}' > /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
