# Constrói o BenchBase (CMU-DB) a partir do fonte, dentro do Docker, para não
# precisar de JDK nem Maven instalados na WSL. O clone acontece durante o
# build: o repositório externo não entra no git deste projeto.
# JDK 23: o pom do BenchBase compila com target 23 (o README ainda
# menciona 21, mas a imagem oficial deles roda em temurin 23).
FROM eclipse-temurin:23-jdk AS build

ARG BENCHBASE_REF=main
ARG BENCHBASE_PROFILE=postgres

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${BENCHBASE_REF}" https://github.com/cmu-db/benchbase.git .

# O wrapper ./mvnw baixa o próprio Maven. -DskipTests porque a suíte sobe
# bancos de verdade para vários dialetos, coisa que não interessa aqui.
RUN ./mvnw -B clean package -P "${BENCHBASE_PROFILE}" -DskipTests

# O tgz extrai para uma pasta com nome próprio. Localiza o diretório pelo jar
# em vez de assumir o nome, que muda entre versões.
RUN mkdir -p /opt/unpack \
 && tar -xzf "target/benchbase-${BENCHBASE_PROFILE}.tgz" -C /opt/unpack \
 && mv "$(dirname "$(find /opt/unpack -name benchbase.jar | head -1)")" /opt/benchbase

FROM eclipse-temurin:23-jre
WORKDIR /benchbase
COPY --from=build /opt/benchbase /benchbase
# O container roda com o uid do host (ver docker-compose) para os arquivos de
# resultado não saírem pertencendo a root na pasta do projeto.
RUN chown -R 1000:1000 /benchbase
ENTRYPOINT ["java", "-jar", "benchbase.jar"]
CMD ["--help"]
