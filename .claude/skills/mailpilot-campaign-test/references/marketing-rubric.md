# Cold-email critique rubric

This rubric guides the Opus critique sub-agent. Judge each sent campaign email on
how well it would perform as real cold outreach to its specific recipient. Every
judgment must be grounded in two things: the recipient's contact context (their
name, role, and what their company does) and the marketing standards below.

The point is not to rewrite the email from scratch. The point is to tell the
operator, per email, what is strong, what is weak, and the single highest-value
change that would lift reply rate.

## Grounding rule

- Read the recipient's contact and company context before scoring. A claim like
  "well personalized" or "too generic" must reference the actual contact -- their
  role, their company, what the company sells.
- Penalize an email that reads as a mail-merge template: a real first name in a
  body that would fit any company is still generic. Reward an opening that could
  only have been written for this recipient.
- If the company context is thin or missing, say so, and judge personalization
  against what was available rather than against an absent profile.

## Dimensions

Score each dimension 1 to 5 (1 = poor, 3 = competent, 5 = excellent).

1. **Relevance and personalization.** Does the email connect the offer to this
   contact's role and this company's actual work? Generic value statements that
   ignore the recipient score low.
2. **Subject line.** Short, specific, and honest. It should earn the open
   without clickbait, ALL CAPS, or false urgency. Vague or spammy subjects score
   low.
3. **Value proposition.** Is the benefit concrete, specific, and led with -- not
   buried under company history? Quantified outcomes beat adjectives.
4. **Credibility and proof.** Are claims believable and backed by specifics
   (numbers, a verifiable artifact) rather than hype? Unsupported superlatives
   score low.
5. **Call to action.** One clear, low-friction next step. Multiple competing
   asks, or a vague "let me know," score low.
6. **Concision and skimmability.** Can a busy reader get the point in a few
   seconds? Reward short paragraphs and a clear structure; penalize walls of
   text and padding.
7. **Tone and register.** Professional, direct, and human. Penalize pushy,
   needy, or over-familiar tone, and penalize stiff corporate filler.
8. **Deliverability and spam risk.** Flag spam-trigger phrasing ("act now",
   "100% free", "guarantee"), excessive links, attachments-language, or
   formatting that filters dislike. Lower risk scores higher.

## Overall score

Give one holistic overall score 1 to 5. It is a judgment, not a strict average:
a fatal weakness (a generic body, a missing CTA, high spam risk) caps the
overall score at 2 even if other dimensions are strong.

## Output per email

For each email, produce:

- `strengths` -- one to three specific things that work, each tied to the email.
- `weaknesses` -- one to three specific problems, each tied to the email.
- `suggestions` -- one to three concrete, actionable changes. The first
  suggestion must be the single highest-impact change.

Keep every line specific to the email under review. Do not restate the rubric.
