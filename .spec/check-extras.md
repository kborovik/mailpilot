# check-extras — audit recipes extracted by /sdd:condense
# Resolved by check-mechanical emit-v-slices when SPEC row stubs `→ .spec/check-extras.md §Vn`.

## §V137

connect-fail operator UX — `_connect_database` maps OperationalError text ordered (role-missing → role/URL not createdb; database-missing → createdb; `no pg_hba.conf entry` → client-host allowlist; resolve/`nodename nor servname`/`Name or service not known` → DNS/hostname; password|Peer auth failed → credentials/auth; Connection refused → service-running; else database_url) → SystemExit hint; expected fail → logfire.error (not exception) + operator_event("error", source="database.connect") + SystemExit — zero console Traceback (closes §B.125)

## §V138

company cohort pipeline status — `company list --status` Enum ∈ {ready, needs_contacts, needs_profile, disabled}; rules: ready = has_profile + contact_count ≥ 1 + not disabled; needs_contacts = has_profile + contact_count = 0 + not disabled; needs_profile = !has_profile + not disabled; disabled = disabled_reason set; Enum family §V.115; `--status disabled` overrides default hide; AND-composes w/ `--tag`/`--no-tag`/`--min/max-contacts`/`--has-profile`/`--include-disabled`; rules in --skill/help (zero SPEC cites §V.111); no `company audit` verb

## §V139

stdin NDJSON batch mutation — selected Mutate verbs accept `--stdin` (NDJSON, 1 object/line); exclusive w/ positional single-entity target (not both); line schema verb-specific (`company disable`: {domain, reason}; `contact create`: create fields + optional `upsert:true` §V.147); envelope always `{"results":[{ref, status}|{ref, status:"error", error, message}], "ok":true, "record_count":N}` full stream (never abort mid-batch w/o reporting prior rows); exit 0 iff zero error rows, exit 1 if any error (still emit full results JSON); per-row errors continue; safe-idempotent defaults: re-disable already-disabled → status ok no-op; duplicate contact natural-key → status ok skip unless line `upsert:true` then field-selective update per §V.147; MVP verbs: `company disable --stdin`, `contact create --stdin`; optional later: `tag add --stdin`, `company update --stdin`; --skill recipes document batch disable + batch contact create; help zero SPEC cites §V.111

## §V140

company profile write paths — `company update` full-replace via exclusive XOR of {`--profile-json`, `--profile-file <path>`, `--profile -` (stdin)}; all three validate vs CompanyProfile (§V.72) before write; field-patch flags {`--summary`, `--product` (multi), `--source` (multi), `--timezone`, `--target-customers`} merge into existing profile (null existing → patch builds base then validates full object); full-replace exclusive w/ any patch flag; invalid → `validation_error` no partial write; success envelope = full company w/ profile (ok:true, record_count=1); --skill recipes prefer file/stdin over inline JSON; help zero SPEC cites §V.111

## §V141

multi-owner tag link + set-replace — `tag add --tag <name>` accepts repeatable `--company-domain` or repeatable `--contact-email`; owner-kind XOR per call (companies or contacts, not mixed; ≥1 owner); undefined tag → `not_found` never auto-create (§V.116); N>1 → results envelope §V.139 shape + exit 0 iff zero errors; N=1 → `tag_assignment` entity envelope; already-linked multi row → status ok skip; `tag set` owner XOR + `--tags` comma-list replaces owner's full assignment set one txn (add missing, remove extras, activity per change §V.14); empty `--tags` clears all; undefined name in set → `not_found` zero writes; company list|view `tags[]` always (§V.8/§V.116); help/--skill zero SPEC cites §V.111

## §V142

company domain aliases — table `company_alias` {domain TEXT UNIQUE NOT NULL lowercased, company_id FK company}; domain space shared: each string is either `company.domain` or `company_alias.domain`, never both + never two owners; `get_company_by_domain` + CLI polymorphic company ref + contact `--company-domain` resolve alias → canonical company (§V.90/§V.107); `company create --alias` (repeatable) registers aliases same txn; create/seed domain already canonical or alias → `already_exists` (no silent second firm); `company view` projects `aliases[]` (sorted, empty ok; list lean omits §V.8); domains lowercased before match+insert; db export/import company.aliases[] (§V.121); migration 011; help/--skill zero SPEC cites §V.111

## §V143

company merge into survivor — `company merge --from <domain|uuid> --into <domain|uuid> [--move-contacts]`; records `from.domain` as alias on into if missing (§V.142); soft-disables from w/ `disabled_reason` = `merged:into <into.domain>` (§V.114); `--move-contacts` reassigns contact.company_id from→into same txn, omit → contacts stay on disabled source; success envelope = survivor company (ok:true, record_count=1, aliases[] incl. from.domain); idempotent already-merged (from disabled w/ matching reason + alias present) → ok no-op; reject self-merge + into disabled → `invalid_state`; missing key → `not_found`; enable of company whose domain is alias of another → `invalid_state` (MVP no alias-remove verb); help/--skill zero SPEC cites §V.111

## §V144

contact operator-only verification meta — JSONB `verification_meta` NULL ok on contact; write via `contact create|update --meta-json` (JSON object not array; invalid → `validation_error`); never written to notes; default ContactView + load_contact_view omit field; `contact view --include-meta` projects `verification_meta` (null when unset); `contact create --stdin` line schema ? optional `meta` object same rules; workflow agent prompt allowlist = {name, title, email, email_confidence, company profile, lean notes ≤ cap} — `verification_meta` never on allowlist; tests pin meta absent from agent context builder path; --skill/help zero SPEC cites §V.111

## §V145

company tracker export — `company export` writes NDJSON (1 company object/line) stable schema keys {domain, name, tags[], has_profile, contact_count, disabled_reason}; `--full` embeds `profile` object or null; filters compose w/ company list family (`--tag`/`--no-tag`/`--status`/`--include-disabled`/`--has-profile`/`--min/max-contacts` §V.138/§V.116/§V.114); domains lowercased; tags sorted; order domain ASC; `--format jsonl` MVP only; `--out <path>` → write file + status envelope on stdout `{"company_export":{path,format,record_count},"ok":true,"record_count":N}` (path null when body on stdout); without `--out` NDJSON body on stdout (stream format exclusion from single-object envelope for tracker pipes; operator lifecycle still stderr §V.3); empty set → zero lines / empty file + record_count 0; not `db export` snapshot (§V.121); --skill schema docs; help zero SPEC cites §V.111

## §V146

company tracker dry-run import — `company import --from <path.jsonl> --dry-run` compares tracker NDJSON to CRM by lowercased domain; dry-run only MVP (no apply writes); optional filters scope CRM side same as export (§V.145); report buckets domain lists: `missing_in_crm` (file not CRM), `missing_profile` (in both or CRM, !has_profile), `zero_contacts` (contact_count=0), `disabled` (disabled_reason set), `extra_in_crm` (CRM scope not in file); envelope `{"company_import_diff":{...},"ok":true,"record_count":N}` (N = |file domains ∪ CRM-scope domains|); missing file → `not_found`; invalid NDJSON line → `validation_error` (no partial report required); --skill bucket docs; help zero SPEC cites §V.111

## §V147

company/contact create upsert — `company create` / `contact create` accept `--upsert`; natural-key conflict w/o flag → existing error codes preserved (`duplicate_key` contact §V.16; `already_exists` company domain/alias §V.142); w/ `--upsert` → field-selective update only supplied flags (contact: title, email_confidence, company_domain if present; ? verification_meta if `--meta-json` present — never clobber omitted; company: name if provided; profile only when `--profile-*` or field-patch flags also passed per §V.140 — bare upsert never wipes profile; new `--alias` ? register missing only, never move ownership); success = final entity envelope + top-level bool `created` (true=insert, false=update) + record_count=1 exit 0; `contact create --stdin` line schema ? optional `upsert:true` same per-row semantics; --skill preferred agent path uses upsert; help zero SPEC cites §V.111

## §V148

company list/search order + page — `company list|search` accept `--sort` Enum ∈ {name, domain, created_at, contact_count} default `name` (ORDER BY LOWER(name) today); `--desc` flips ASC→DESC; `--offset N` default 0 + `--limit` default 500 (tag-cohort sized; other nouns keep 100 per §V.115); invalid sort → `validation_error`; `record_count` = page length only (no total/has_more MVP); list filters unchanged (§V.138/§V.116/§V.114/§V.96); search text-match name/domain/alias; lean row list|search same fields {domain, name, has_profile, contact_count, tags[], disabled_reason}; stdout one JSON document, diagnostics stderr (§V.3); --skill docs defaults + sort keys; help zero SPEC cites §V.111

## §V149

disable reason-file — `company disable` + `contact disable` accept `--reason-file <path>` XOR `--reason` (exactly one reason source in single-entity mode); file UTF-8, strip one trailing newline, empty → `validation_error`; missing path → `not_found`; `--stdin` exclusive w/ both reason sources (company disable batch still per-line reason); reason ! empty after resolve; success envelope unchanged; --skill; help zero SPEC cites §V.111

## §V150

enrollment tag-cohort dry-run — `enrollment add --workflow-id <ref> --tag <name> --dry-run` [optional `--min-contacts N`]; company-tag path only MVP; dry-run required for tag path (tag w/o dry-run → `validation_error`; dry-run w/o tag → `validation_error`); single-contact `--contact-email` path unchanged (no dry-run needed); expand: companies w/ tag (disabled excluded by default §V.114) → enabled contacts; drop already-enrolled for workflow + self-loop contacts (§V.33) + disabled contacts; optional `--min-contacts N` filters companies before expand; envelope `{"enrollment_preview":{workflow, tag, count, contacts:[{email, company_domain}], excluded:{disabled_companies, already_enrolled, self_loop, disabled_contacts}},"ok":true,"record_count":count}` (aggregate not enrollment row); undefined tag → `not_found`; zero candidates → ok empty record_count=0; no writes; --skill cohort recipe; help zero SPEC cites §V.111
