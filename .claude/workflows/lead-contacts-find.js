export const meta = {
  name: 'lead-contacts-find',
  description: 'Concurrently discover + verify decision-maker contacts for stale companies via contact-finder agents',
  whenToUse: 'After lead-companies enriches company profiles: discover and verify <=5 decision-maker contacts for every profile-bearing company with fewer than 5 contacts. Invoked by the lead-contacts skill; args = the stale-row array {id, domain, name}.',
  phases: [{title: 'Discover', detail: 'contact-finder agents, 3 in flight'}],
}

// `stale` source: the discover set per V.96 -- companies w/ profile IS NOT NULL
// and < 5 existing contacts (count includes disabled rows so memoization holds),
// captured by the skill's stale-query and handed in via Workflow `args`. The
// runtime delivers `args` as a JSON string, so parse it (guard the already-parsed
// case). To paste rows directly instead, replace this line with an inline
// literal: const stale = [{...}, ...].
const stale = typeof args === 'string' ? JSON.parse(args) : args

const CONTACT_RESULT_SCHEMA = {
  type: 'object',
  required: ['company_id', 'domain', 'status'],
  properties: {
    company_id: {type: 'string'},
    domain: {type: 'string'},
    status: {enum: ['seeded', 'skipped', 'failed']},
    contacts_created: {type: 'integer'},
    flagged: {type: 'integer'},
    reason: {type: 'string'},
    reason_code: {enum: ['no_decision_makers', 'all_already_seeded', 'transient']},
  },
}

function buildPrompt(c) {
  return [
    'Discover decision-maker contacts for:',
    `  company_id: ${c.id}`,
    `  domain: ${c.domain}`,
    `  company_name: ${c.name}`,
    '',
    'Follow your system prompt procedure. Return the JSON verdict per spec.',
  ].join('\n')
}

phase('Discover')

// Chunk into batches of 3 so at most 3 contact-finder agents run at once -- this
// (not the runtime cap) is what honors the concurrency-3 budget (V.96/V.73). A
// bare parallel(stale.map(...)) would submit all stale.length at once, bounded
// only by the runtime cap min(16, cores-2).
const results = []
for (let i = 0; i < stale.length; i += 3) {
  const batch = stale.slice(i, i + 3)
  const batchResults = await parallel(batch.map(c => () =>
    agent(buildPrompt(c), {
      label: `contacts:${c.domain}`,
      agentType: 'contact-finder',
      schema: CONTACT_RESULT_SCHEMA,
    })
  ))
  results.push(...batchResults)
}
return results.filter(Boolean)
