CREATE TABLE tenants (id uuid PRIMARY KEY, enabled boolean NOT NULL DEFAULT true);
CREATE TABLE memberships (
 tenant_id uuid NOT NULL REFERENCES tenants(id), subject text NOT NULL,
 role text NOT NULL CHECK (role IN ('reader','editor','deployer','owner')), active boolean NOT NULL DEFAULT true,
 PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE resources (
 tenant_id uuid NOT NULL REFERENCES tenants(id), id uuid NOT NULL, kind text NOT NULL,
 data jsonb NOT NULL, revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, id)
);
CREATE INDEX resources_kind ON resources(tenant_id, kind, created_at, id);
CREATE TABLE resource_links (
 tenant_id uuid NOT NULL, parent_id uuid NOT NULL, relation text NOT NULL, child_id uuid NOT NULL,
 PRIMARY KEY (tenant_id, parent_id, relation),
 FOREIGN KEY (tenant_id, parent_id) REFERENCES resources(tenant_id,id),
 FOREIGN KEY (tenant_id, child_id) REFERENCES resources(tenant_id,id)
);
CREATE INDEX resource_links_child ON resource_links(tenant_id,child_id);
CREATE TABLE operations (
 tenant_id uuid NOT NULL REFERENCES tenants(id), id uuid NOT NULL UNIQUE,
 kind text NOT NULL, state text NOT NULL, request jsonb NOT NULL, requester text NOT NULL,
 scopes jsonb NOT NULL, result jsonb, error jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
 deadline timestamptz NOT NULL, cancel_requested boolean NOT NULL DEFAULT false,
 event_seq bigint NOT NULL DEFAULT 0, execution_id uuid, fence bigint NOT NULL DEFAULT 0,
 PRIMARY KEY (tenant_id,id), FOREIGN KEY (tenant_id,requester) REFERENCES memberships(tenant_id,subject),
 CHECK (state IN ('queued','running','waiting_for_policy','reconciling','needs_attention','succeeded','failed','cancelled'))
);
CREATE INDEX operations_state ON operations(tenant_id,state,created_at);
CREATE TABLE operation_links (
 tenant_id uuid NOT NULL, operation_id uuid NOT NULL, relation text NOT NULL, resource_id uuid NOT NULL,
 PRIMARY KEY (tenant_id,operation_id,relation),
 FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id),
 FOREIGN KEY (tenant_id,resource_id) REFERENCES resources(tenant_id,id)
);
CREATE INDEX operation_links_resource ON operation_links(tenant_id,resource_id);
CREATE TABLE idempotency_keys (
 tenant_id uuid NOT NULL, route text NOT NULL, key text NOT NULL, body_digest text NOT NULL,
 operation_id uuid NOT NULL, expires_at timestamptz NOT NULL,
 PRIMARY KEY (tenant_id,route,key), FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id)
);
CREATE INDEX idempotency_expiry ON idempotency_keys(tenant_id,expires_at);
CREATE TABLE outbox (
 tenant_id uuid NOT NULL, id uuid NOT NULL, operation_id uuid NOT NULL, kind text NOT NULL DEFAULT 'start',
 state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
 lease_until timestamptz, lease_id uuid, last_error text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,id), FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id)
);
CREATE INDEX outbox_pending ON outbox(tenant_id,state,lease_until,created_at);
CREATE TABLE events (
 tenant_id uuid NOT NULL, operation_id uuid NOT NULL, seq bigint NOT NULL, name text NOT NULL,
 data jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,operation_id,seq), FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id)
);
CREATE TABLE step_journal (
 tenant_id uuid NOT NULL, operation_id uuid NOT NULL, key text NOT NULL, input_digest text NOT NULL,
 state text NOT NULL CHECK (state IN ('intent','complete')), receipt jsonb,
 PRIMARY KEY (tenant_id,operation_id,key), FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id)
);
CREATE TABLE application_claims (
 tenant_id uuid NOT NULL, application_id uuid NOT NULL, target_id uuid NOT NULL,
 operation_id uuid NOT NULL, execution_id uuid NOT NULL, fence bigint NOT NULL,
 PRIMARY KEY (tenant_id,application_id,target_id),
 FOREIGN KEY (tenant_id,application_id) REFERENCES resources(tenant_id,id),
 FOREIGN KEY (tenant_id,target_id) REFERENCES resources(tenant_id,id),
 FOREIGN KEY (tenant_id,operation_id) REFERENCES operations(tenant_id,id)
);
CREATE TABLE resource_bindings (
 tenant_id uuid NOT NULL, application_id uuid NOT NULL, target_id uuid NOT NULL, key text NOT NULL,
 native_id text NOT NULL, resource_type text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY (tenant_id,application_id,target_id,key),
 UNIQUE (tenant_id,target_id,resource_type,native_id),
 FOREIGN KEY (tenant_id,application_id) REFERENCES resources(tenant_id,id),
 FOREIGN KEY (tenant_id,target_id) REFERENCES resources(tenant_id,id)
);
CREATE TABLE managed_namespaces (
 host text PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['memberships','resources','resource_links','operations','operation_links','idempotency_keys','outbox','events','step_journal','application_claims','resource_bindings','managed_namespaces'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = nullif(current_setting(''shifde.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''shifde.tenant_id'', true), '''')::uuid)', t);
 END LOOP;
END $$;
