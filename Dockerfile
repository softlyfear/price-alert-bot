FROM ubuntu:latest
LABEL authors="Softly"

ENTRYPOINT ["top", "-b"]