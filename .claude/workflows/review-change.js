export const meta = {
  name: 'review-change',
  description: 'Review the current branch with the project reviewers, then have a fresh agent try to refute each finding',
  whenToUse: 'Before opening a pull request, or when asked for a thorough review of the branch. Optional args: {"base": "origin/main"}',
  phases: [
    { title: 'Review', detail: 'one specialist reviewer per concern' },
    { title: 'Verify', detail: 'an independent agent tries to refute each finding' },
  ],
}

// The project's reviewer agents, defined in .claude/agents/. Each one reports only on its own concern.
const REVIEWERS = ['architecture-reviewer', 'detector-reviewer', 'portability-reviewer', 'privacy-reviewer']

const base = args && args.base ? args.base : 'origin/main'

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'line', 'summary', 'evidence', 'fix'],
        properties: {
          file: { type: 'string', description: 'Path relative to the repository root' },
          line: { type: 'integer' },
          summary: { type: 'string', description: 'One sentence: what is wrong' },
          evidence: { type: 'string', description: 'The input, command or code path that shows it' },
          fix: { type: 'string', description: 'The smallest change that fixes it' },
        },
      },
    },
  },
}

const VERDICT = {
  type: 'object',
  required: ['real', 'reason'],
  properties: {
    real: { type: 'boolean', description: 'true only if you reproduced or confirmed the problem yourself' },
    reason: { type: 'string', description: 'What you ran or read, and what it showed' },
  },
}

const reviewPrompt = `Review the changes on the current branch against ${base}.

Get them with "git diff ${base}...HEAD" for committed work and "git diff HEAD" for uncommitted work. Read every changed file in full, not only the changed lines.

Follow your own instructions for what to look for and how to check it. Report each problem as one finding with the file, the line, what is wrong, the evidence, and the smallest fix. If nothing in the changes falls in your area, or you find nothing that affects correctness or the project's stated rules, return an empty list. Do not report style preferences. Do not edit any file.`

const verifyPrompt = (finding, reviewer) => `A reviewer (${reviewer}) reported this problem in the changes on the current branch (compared with ${base}). Try to refute it.

File: ${finding.file}:${finding.line}
Claim: ${finding.summary}
Evidence given: ${finding.evidence}
Suggested fix: ${finding.fix}

Read the code yourself and check the claim independently: run the snippet or command, read the code path, check the fact. Do not trust the evidence as written. Answer real=true only if you confirmed the problem yourself and it affects correctness, false positives, portability, privacy, or a rule in CLAUDE.md or .claude/rules/. If you could not confirm it, or it is a matter of taste, answer real=false. Do not edit any file.`

// The agents in .claude/agents/ are loaded when a session starts. In a session that began before
// they existed, run the same review from the agent's file instead of failing.
const review = async reviewer => {
  const options = { label: `review:${reviewer}`, phase: 'Review', schema: FINDINGS }
  try {
    return await agent(reviewPrompt, { ...options, agentType: reviewer })
  } catch (error) {
    if (!String(error && error.message).includes('not found')) throw error
    log(`${reviewer} is not loaded in this session; using .claude/agents/${reviewer}.md directly`)
    const asReviewer = `Read .claude/agents/${reviewer}.md first. It defines your role for this task: follow its instructions exactly.\n\n${reviewPrompt}`
    return await agent(asReviewer, options)
  }
}

const reviewed = await pipeline(
  REVIEWERS,
  review,
  (result, reviewer) => {
    const findings = result && result.findings ? result.findings : []
    return parallel(
      findings.map(finding => () =>
        agent(verifyPrompt(finding, reviewer), {
          label: `verify:${finding.file}:${finding.line}`,
          phase: 'Verify',
          schema: VERDICT,
        }).then(verdict => ({ ...finding, reviewer, real: Boolean(verdict && verdict.real), check: verdict ? verdict.reason : 'not verified' })),
      ),
    )
  },
)

const all = reviewed.flat().filter(Boolean)
const failedReviewers = REVIEWERS.filter((_, index) => reviewed[index] === null)
if (failedReviewers.length === REVIEWERS.length) throw new Error('No reviewer finished, so nothing was reviewed.')
if (failedReviewers.length) log(`reviewers that did not finish: ${failedReviewers.join(', ')}`)
log(`${all.filter(f => f.real).length} of ${all.length} findings confirmed`)

return {
  base,
  confirmed: all.filter(f => f.real),
  rejected: all.filter(f => !f.real).map(f => ({ file: f.file, line: f.line, summary: f.summary, why: f.check })),
  failedReviewers,
}
