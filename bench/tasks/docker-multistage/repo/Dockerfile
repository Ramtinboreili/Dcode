FROM python:latest
RUN apt-get update && apt-get install -y gcc libpq-dev make
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
RUN make -C native
ENV PYTHONPATH=/app
EXPOSE 9000
CMD python -m service
