# Review execution cost comparison

**Checked:** 2026-10-09. Prices are in USD and may change. This comparison is for planning the private `lispz1610/open-code-review-setup` project; it does not claim that a hosting provider has been selected.

## Assumptions and calculation

- One review run takes 10 billed minutes. Actual runtime and the number of runs per pull request must be measured in the live acceptance test.
- The Actions option uses a standard GitHub-hosted Linux x64 2-core runner at [$0.006 per minute](https://docs.github.com/en/billing/reference/actions-runner-pricing). GitHub rounds each job up to a whole minute. Private repositories draw from an account-level monthly included allowance before overage is charged; public repositories using standard runners incur no runner-minute charge. Other workflows share the private account's allowance. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
- The [GitHub Student Developer Pack](https://education.github.com/pack) includes GitHub Pro while student status is active. GitHub Pro includes 3,000 standard Actions minutes per month for the **personal account**, shared across its private repositories and workflows; it is not 3,000 minutes per repository. A repository owned by an organization uses that organization's plan and billing allowance. At 10 minutes per review, 3,000 otherwise unused minutes could cover about 300 review runs, before CI or other private-repository jobs consume any. Without a payment method, GitHub blocks further metered Actions usage once the included quota is exhausted. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
- For an illustrative external GitHub App worker, assume a Google Cloud Run job in the `us-central1` price tier allocated 2 vCPU and 4 GiB during the same 10-minute run. The published on-demand rates are [$0.000018 per vCPU-second and $0.000002 per GiB-second](https://cloud.google.com/run/pricing). The published monthly free tier is 240,000 vCPU-seconds and 450,000 GiB-seconds, shared with other eligible Cloud Run use on the billing account. Region, configuration, and other workloads change the result. The table's pre-allowance figure uses `(2 × 0.000018 + 4 × 0.000002) × 600 seconds = $0.0264` per review. The post-allowance illustration assumes the entire free tier is available to these jobs.
- These are **compute-only** figures. They exclude provider tokens, data transfer, logs, secret storage, queueing, webhook ingress, image storage, taxes, and engineer time. Cloud Run is only a representative metered host, not a proposed deployment target.

| Runs/month | Actions minutes | Actions if allowance exhausted | App worker compute before free tier | App worker compute if its full free tier is available |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 100 | $0.60 | $0.26 | $0.00 |
| 100 | 1,000 | $6.00 | $2.64 | $0.00 |
| 1,000 | 10,000 | $60.00 | $26.40 | $21.18 |

These figures are not a full service quote. In the App option, an HTTP service must receive GitHub webhooks and acknowledge them within 10 seconds, while a queue and worker process reviews asynchronously. [GitHub's webhook guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks) also calls for delivery deduplication and recovery of missed deliveries. The service must verify signatures, manage App installation tokens and private keys, store provider credentials, post comments, and be monitored. Hosting, operation, and incident response can outweigh a small runner-minute saving. A private GitHub App registration is installable only on its owning account; serving other accounts requires a public App registration. [GitHub App visibility](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private).

For equal model, prompt, and repository content, LLM usage is approximately the same in both options. Its bill depends on provider prices and actual input/output tokens, neither of which is known. A requested token budget is not a hard billing cap. Opening, updating, and reopening a pull request can produce several runs; cancellation does not refund tokens already sent to the provider.

The review integration is bring-your-own-key (BYOK). The Actions workflow passes the owner's provider endpoint, model name, and repository secret to the pinned [OpenCodeReview Action](https://github.com/alibaba/open-code-review/blob/182898cf522da3d04157b422752d028417974e19/action.yml). GitHub Actions supplies execution time, not an included LLM. [Copilot Student model-selection limits](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/configure-access-to-ai-models) do not constrain this integration because it does not call Copilot. The provider must grant API access to the chosen model, and its API quota and charges apply in either the Actions or hosted-App architecture. For example, [OpenAI API billing is separate from a ChatGPT subscription](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform).

## Decision inputs still needed

- Which target repositories are private, and is their current Actions allowance already consumed by CI?
- Is a target repository's Actions policy known to block the pinned review Action or one of its dependencies?
- How many review runs occur per month, and how long do they take on a representative pull request?
- Which provider, model, and approximate tokens per run are expected?
- If an external App is selected, which hosting platform, region, credential store, queue, and on-call owner will be used?

Until those measurements exist, the comparison supports an operational tradeoff, not a precise break-even point. The first version will use GitHub Actions. A GitHub App remains an alternative if a real target repository is blocked by Actions policy or measured volume warrants a hosted service.
