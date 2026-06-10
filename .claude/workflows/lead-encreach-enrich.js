export const meta = {
  name: 'lead-encreach-enrich',
  description: 'Concurrently enrich stale company profiles via company-profiler agents',
  whenToUse: 'After lead-encreach seeds company rows: enrich every profile-NULL row to a cold-email-grade CompanyProfile. Invoked by the lead-encreach skill; args = the stale-row array {id, domain, name}.',
  phases: [{title: 'Enrich', detail: 'company-profiler agents, 3 in flight'}],
}

// `stale` source: the `companies[]` captured from the seed script's `stale`
// field (or `mailpilot company list --no-profile`), handed in via Workflow
// `args`. The runtime delivers `args` as a JSON string, so parse it (guard the
// already-parsed case). To paste rows directly instead, replace this line with
// an inline literal: const stale = [{...}, ...].
const stale = typeof args === 'string' ? JSON.parse(args) : args

const ENRICH_RESULT_SCHEMA = {
  type: 'object',
  required: ['company_id', 'domain', 'status'],
  properties: {
    company_id: {type: 'string'},
    domain: {type: 'string'},
    status: {enum: ['enriched', 'skipped', 'failed']},
    reason: {type: 'string'},
  },
}

function buildPrompt(c) {
  return [
    'Enrich the company profile for:',
    `  company_id: ${c.id}`,
    `  domain: ${c.domain}`,
    `  placeholder_name: ${c.name}`,
    '',
    'Follow your system prompt procedure. Return the JSON verdict per spec.',
  ].join('\n')
}

phase('Enrich')

// Chunk into batches of 3 so at most 3 enricher agents run at once -- this
// (not the runtime cap) is what honors the concurrency-3 budget (V.72/V.73). A
// bare parallel(stale.map(...)) would submit all stale.length at once, bounded
// only by the runtime cap min(16, cores-2).
const results = []
for (let i = 0; i < stale.length; i += 3) {
  const batch = stale.slice(i, i + 3)
  const batchResults = await parallel(batch.map(c => () =>
    agent(buildPrompt(c), {
      label: `enrich:${c.domain}`,
      agentType: 'company-profiler',
      schema: ENRICH_RESULT_SCHEMA,
    })
  ))
  results.push(...batchResults)
}
return results.filter(Boolean)
