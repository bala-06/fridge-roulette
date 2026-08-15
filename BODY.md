## Vision & What the App Does

Every evening has the same small problem in it. You open the fridge, find half a cabbage, some
paneer and two tomatoes, and lose ten minutes deciding what to do with them. Recipe sites are no
help, because they work the wrong way round — they tell you what to buy. I wanted something that
starts from what is already in front of you.

**Fridge Roulette** takes a plain sentence about your leftovers and invents dinner. You type
*"half a cabbage, some paneer, two tomatoes"*, choose how much time you have and whether you want
it vegetarian, and it hands back a complete dish: a name it made up, four to six steps, a cook
time, a serving count, an honest list of anything you are missing, and swaps for what you do not
have.

The creative output is the recipe itself. Nothing is looked up. There is no database of dishes
behind this, no table of ingredient combinations. Every dish is invented on the spot by a language
model working inside the constraints you set, right down to naming it. Ask the same question twice
and you get two different dinners. *"Spicy Cabbage and Potato Stir-Fry"* one time, something else
the next.

Three details make it feel less like a demo and more like a tool. The **time budget** genuinely
changes the recipe rather than just labelling it — ten minutes gets you assembly, forty gets you
something simmered. **Use-it-up mode** tells it to prioritise whatever spoils first, which is the
actual reason most of us are staring into the fridge in the first place. And the **missing list**
is honest: if the dish really needs one thing you did not mention, it says so instead of silently
pretending you own it.

## How You Built It

I gave myself a rule at the start: one clear job, done properly, rather than four half-features.
That rule decided nearly everything else.

The whole backend is a single Python file with no dependencies beyond the `boto3` already present
in the Lambda runtime. There is no build step, no bundler, no framework on the frontend either —
one HTML file with inline CSS and JavaScript. For an app whose entire job is "take a sentence,
return a recipe", anything more would have been ceremony.

**The real engineering problem was not calling the model. It was trusting what came back.**

A language model asked to produce JSON will comply almost every time, and then occasionally wrap it
in a code fence, or add a cheerful sentence before it, or return `time_mins` as the string
`"20 minutes"` instead of a number. Any one of those breaks a UI that assumed it could just render
the response. I dealt with it in three layers.

First, the prompt carries one worked example of the exact shape I want, with every field filled in.
Showing the model the target is far more reliable than describing it.

Second, parsing takes everything between the first `{` and the last `}` rather than trusting the
whole response to be clean JSON. Stray prose and code fences stop mattering.

Third — and this is the layer that actually saved me — nothing from the model reaches the UI
untouched. A `_clean` function coerces every field to the type the frontend expects. Lists stay
lists, numbers that arrive as text fall back to sensible defaults, and a missing dish name becomes
*"Fridge Surprise"*. The only genuinely fatal case is a recipe with no steps at all, which is the
one thing I cannot paper over.

The result is that a slightly misbehaving model produces a slightly plainer recipe instead of a
broken page.

Two smaller things caught me out. The Lambda **default timeout of three seconds is not enough** for
a Bedrock round trip, which takes one to four seconds — that needs raising to 30 or you will chase
phantom failures. And every input is validated *before* it reaches the model: an empty fridge, a
500-character essay, or a nonsense time budget is rejected or defaulted at the edge. Validating
early keeps junk from ever becoming a billable model call.

## AWS Services Used / Architecture Overview

```
Browser  ──HTTPS──>  CloudFront  ──>  S3 (static site)
   │
   └──POST──>  API Gateway (HTTP API)  ──>  Lambda (Python 3.13)  ──>  Amazon Bedrock
                                                                        Nova Lite
```

| Service | What it does here |
|---|---|
| **Amazon Bedrock** | Nova Lite invents the dish and returns it as structured JSON |
| **AWS Lambda** | Builds the prompt, calls Bedrock, parses and validates the response |
| **Amazon API Gateway** | HTTP API with CORS, and the throttle that caps my spend |
| **Amazon S3** | Hosts the static frontend |
| **Amazon CloudFront** | HTTPS and the custom domain |

**Why Nova Lite.** The workload is small and structured — about 200 tokens in, 170 out per recipe.
Nova Lite is the cheapest model in the family that reliably holds a JSON shape at that size, which
works out to roughly **$0.05 per 1,000 recipes**. Nova Pro costs around thirteen times as much and
would not invent a better stir-fry from half a cabbage. Picking the smallest model that clears the
bar is most of cost control on Bedrock.

**Serverless because the traffic shape demands it.** This app is idle almost all the time and then
busy for a few seconds at dinner time. There is no server to keep warm, no capacity to plan, and
nothing to pay for between requests. The entire architecture scales to zero.

**The throttle is part of the design, not an afterthought.** A public endpoint that calls a paid
model on every request is exactly how people wake up to surprise bills. API Gateway is capped at
5 requests per second with a burst of 10, which bounds the worst case hard while being far above
anything a real user needs.

## What You Learned

**Inference profiles are not optional in every region.** In `ap-south-1`, every Nova model is
available only through a cross-region inference profile. Calling the bare model id
`amazon.nova-lite-v1:0` is rejected outright. You must use the regional profile:

```
apac.amazon.nova-lite-v1:0
```

This is the single most useful thing I learned, and it is easy to lose an afternoon to if you are
following a tutorial written for `us-east-1`.

**Cross-region inference changes what IAM needs.** Because the call may actually execute in a
different region, permission on the inference profile alone is not enough. The policy needs both
the profile ARN and the underlying foundation model ARN with a wildcard region:

```json
"Resource": [
  "arn:aws:bedrock:ap-south-1:<account-id>:inference-profile/apac.amazon.nova-lite-v1:0",
  "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0"
]
```

**Know your real ceiling.** I set an API Gateway throttle at 5 requests per second and assumed that
was my limit. It is not. The Nova Lite cross-region quota in my account is **40 requests per
minute** — roughly 0.67 per second, about seven times tighter than the throttle sitting above it.
The binding constraint in a serverless stack is rarely the layer you configured yourself. Worth
finding out before your users do, and worth handling gracefully: throttling surfaces to the user as
*"The kitchen is busy, try again in a moment"* rather than a stack trace.

**Prompt engineering is schema engineering.** I spent more time on the shape of the response than
on the wording of the request. Giving the model one concrete example of the JSON I wanted did more
for reliability than any amount of instruction, and validating everything on the way out meant the
occasional bad response degraded quietly instead of breaking the page.

## Link to App or Repo

**Live app:** https://fridge.balakrishnan.me

**Source:** https://github.com/bala-06/fridge-roulette

No sign-up, nothing to install. The page loads with an example fridge already filled in, and there
are one-click sample fridges under the box if you would rather not type — click one and it cooks
immediately.
