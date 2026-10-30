# GCP Cloud Run

Create the Secret Manager entries, then submit the Cloud Build pipeline:

```bash
gcloud services enable cloudbuild.googleapis.com run.googleapis.com secretmanager.googleapis.com
printf "falkordb-host.example.internal" | gcloud secrets create falkordb-host --data-file=-
printf "6379" | gcloud secrets create falkordb-port --data-file=-
# Set _ALLOWED_ORIGINS to your actual service URL after the first deploy.
gcloud builds submit --config deploy/gcp/cloudbuild.yaml \
  --substitutions _REGION=us-central1,_SERVICE_NAME=knowledge-explorer,_ALLOWED_ORIGINS=https://knowledge-explorer-REPLACE_ME.a.run.app
```

For declarative deploys, substitute your project ID and deploy in one step:

```bash
sed "s/PROJECT_ID/$(gcloud config get-value project)/g" deploy/gcp/cloudrun-service.yaml | \
  gcloud run services replace - --region us-central1
```

## Keeping the graph across restarts

The Explorer holds its graph in memory, so Cloud Run restarts and scale-to-zero
would wipe it. Set `SEMANTICA_GRAPH_PATH` to a file on a mounted Cloud Storage
bucket and the Explorer loads it at start-up, saves it every
`SEMANTICA_GRAPH_SAVE_INTERVAL` seconds (default 30) when the graph changed, and
saves once more on shutdown (Cloud Run sends SIGTERM first).

```bash
gsutil mb -l us-central1 gs://$(gcloud config get-value project)-semantica-data
gcloud run services update knowledge-explorer --region us-central1 \
  --max-instances 1 \
  --add-volume name=data,type=cloud-storage,bucket=$(gcloud config get-value project)-semantica-data \
  --add-volume-mount volume=data,mount-path=/data \
  --update-env-vars SEMANTICA_GRAPH_PATH=/data/graph.json
```

The service account needs `roles/storage.objectUser` on the bucket.

- **Keep `--max-instances 1`.** Each instance has its own copy of the graph, so two instances would overwrite each other's saves.
- **At most one autosave interval of changes can be lost** if an instance is killed without a clean shutdown.
- **Only the graph is saved.** Annotations are not; provenance has its own `SEMANTICA_PROVENANCE_DB`.
- A graph file that exists but cannot be read stops start-up instead of starting empty, so a bad file is never overwritten by the autosave.
