git checkout main
git pull
docker compose -f docker-compose.yml  stop
#rebuild
# docker-compose -f docker-compose.yml -f docker-compose.production.yml down --rmi all --volumes --remove-orphans
# docker-compose -f docker-compose.yml -f docker-compose.production.yml build --no-cache
docker compose -f docker-compose.yml  up -d