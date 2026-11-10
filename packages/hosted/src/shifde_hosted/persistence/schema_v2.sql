CREATE TABLE worker_pools (
 tenant_id uuid NOT NULL REFERENCES tenants(id), id uuid NOT NULL,
 data jsonb NOT NULL, PRIMARY KEY (tenant_id,id)
);
CREATE TABLE executors (
 tenant_id uuid NOT NULL, id uuid NOT NULL, pool_id uuid NOT NULL,
 subject text NOT NULL, enabled boolean NOT NULL DEFAULT true,
 PRIMARY KEY (tenant_id,id), UNIQUE (tenant_id,subject),
 FOREIGN KEY (tenant_id,pool_id) REFERENCES worker_pools(tenant_id,id)
);
CREATE TABLE execution_jobs (
 tenant_id uuid NOT NULL, id uuid NOT NULL, operation_id uuid NOT NULL, pool_id uuid NOT NULL,
 action text NOT NULL, data jsonb NOT NULL, state text NOT NULL DEFAULT 'queued',
 worker_id uuid, lease_id uuid, lease_request_id uuid, fence bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 sequence bigint NOT NULL DEFAULT 0, result jsonb, outputs jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,id),
 FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id),
 FOREIGN KEY (tenant_id,pool_id) REFERENCES worker_pools(tenant_id,id),
 FOREIGN KEY (tenant_id,worker_id) REFERENCES executors(tenant_id,id),
 CHECK (state IN ('queued','leased','succeeded','failed','lost'))
);
CREATE INDEX execution_jobs_poll ON execution_jobs(tenant_id,pool_id,created_at) WHERE state='queued';
CREATE INDEX execution_jobs_operation ON execution_jobs(tenant_id,operation_id);
CREATE TABLE execution_checkpoints (
 tenant_id uuid NOT NULL, job_id uuid NOT NULL, sequence bigint NOT NULL,
 digest text NOT NULL, response jsonb NOT NULL,
 PRIMARY KEY (tenant_id,job_id,sequence),
 FOREIGN KEY (tenant_id,job_id) REFERENCES execution_jobs(tenant_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['worker_pools','executors','execution_jobs','execution_checkpoints'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = nullif(current_setting(''shifde.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''shifde.tenant_id'', true), '''')::uuid)',t);
 END LOOP;
END $$;
