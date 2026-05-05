FROM locustio/locust:2.43.4

USER root
COPY locust/ /opt/locust/
RUN pip install --no-cache-dir -r /opt/locust/requirements.txt
USER locust

WORKDIR /opt/locust
ENV LOCUST_LOCUSTFILE=/opt/locust/locustfile.py
