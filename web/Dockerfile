FROM nginx:1.27-alpine
RUN rm /etc/nginx/conf.d/default.conf && mkdir -p /etc/nginx/snippets /public && chown 1000:1000 /public
COPY nginx.conf /etc/nginx/conf.d/buddy.conf
COPY headers.conf /etc/nginx/snippets/headers.conf
COPY static/ /usr/share/nginx/html/
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
