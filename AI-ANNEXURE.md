# AI workflow annexure

Project: Portfolio Tracker, daily brief and intraday market monitor

Roughly 20 hours of work, of which about 15 was troubleshooting rather than building. That
ratio is the honest headline. The first working version came together quickly. Making it
trustworthy against a service with no API, no documentation and no obligation to stay still
took everything after that.

Built with AI pair programming (Claude), used the way an editor uses a first draft rather
than accepting output uncritically. The record below is weighted towards what broke,
because that is where the division of labour is actually visible. Code that works first
time proves very little about who understood it.

Portfolio figures and holdings are omitted throughout. This is a public repository.

---

## Reverse engineering the login

EasyEquities has no official API or public documentation, so the sign-in flow had to be
worked out from the real form. AI drafted the two-step EasyID submission approach. Getting
it to actually work was mine. The drafted version did not survive contact with the live
site, and fixing it meant reading the real HTML responses, working out which hidden fields
had to be carried between steps, and finding that posting the visible fields empty made the
service treat it as a failed login attempt rather than a partial one.

That is the pattern for most of this project. The shape of the solution came quickly. The
part that made it work came from watching what the server actually did.

---

## What AI could not have caught on its own

**A silent failure found by checking the real account, not the code.**
The brief one morning reported one of my holdings at roughly half its actual value. The
code had not crashed, warned, or logged anything. The sync had failed, the script had
quietly fallen back to hardcoded positions several weeks old, and it then presented those
numbers with complete confidence. Nothing in the output looked wrong.

It was caught only because I compared it against my account. That drove the change that
matters most here. The brief now states plainly when it is running on old positions, in
both the Markdown and HTML output, instead of looking identical whether the data is fresh
or stale.

**Scraping data that no longer existed.**
For a long time the system would happily pull and report on positions that were no longer
held. Because the output was well formed, this looked like working software. Holdings are
now read from a single source of truth file, so a position that is gone stops appearing.

**A login that reported success while failing.**
The login function returned without error, so every caller believed it had worked. It had
not. The service was accepting the credentials, issuing a redirect, then immediately
sending the session to a sign-out endpoint. The underlying library failed several steps
later on a bare assertion with no message, so the error surfaced as an empty string.

Diagnosing it meant tracing the redirect chain hop by hop against the live site rather than
reasoning about the code. The fix was to make the login verify it can actually reach the
account before claiming success, so the failure names itself.

**News confidently attached to the wrong company.**
The per-ticker news feed returned stories about an LNG shipping company and an oilfield
services firm under a solar manufacturer, and a generic strong sell list under a retailer.
The code was correct. The API was simply loose. Only visible by reading real output against
real holdings, and now filtered so a headline must actually name the company.

**News for stocks I no longer owned.**
The brief was pulling headlines for four tickers sold months earlier, because the list was
hand maintained and had gone stale. It is now derived from current holdings, so selling
something stops its news automatically.

**A scheduled snapshot that could never fire.**
The monitor was meant to produce a closing snapshot at the market close. It never once did.
Markers were evaluated only after the check for whether a session was open, so the tick
before the bell was too early and the tick after it exited on markets closed. With a twenty
minute cadence nothing could land on the close exactly. Invisible in code review. It
surfaced only by waiting for an output that had been promised and never arrived.

**The yfinance response shape changed.**
Headlines moved under `item["content"]` instead of being flat. `fetch_stock_headlines`
keeps a fallback for both shapes. This is the class of break that AI assisted code does not
catch until it runs against the live API.

---

## Where AI was wrong and I corrected it

Working from the stale data described above, the assistant concluded that one of my
positions had halved and asked me to confirm a sale. No sale had happened. The input was
wrong, so the reasoning built on it was wrong, and rejecting it took my own knowledge of my
account. Plausible reasoning over bad data is still bad output, and it does not look any
different from the correct kind.

---

## A decision that was mine

The service eventually began refusing programmatic sessions altogether. There was a
technical route to keep going, because the block was behavioural rather than absolute. I
chose not to take it.

The library scrapes their web platform, spoofing a browser user agent, and there is no
official API behind it. Once the provider was clearly saying no, continuing would have
meant working around access controls on my own financial account. The automated sync was
retired instead. Holdings now come from a file I maintain and confirm against the app,
which costs me convenience and is the right call regardless.

That is the kind of judgement the tooling has no basis to make.

---

## What the automation cost

Rapid automated iteration is what triggered the lockout. Testing drove more than ten logins
inside an hour, which is a pattern their security responds to, and I have had to sign in by
hand to clear it since. The naive fix made it worse, because retrying three times per run
meant the system knocked hardest exactly when it was least welcome. It now backs off for a
full hour after a failed login.

The intraday monitor also runs inside a session on my own machine. On one occasion it
queued around thirty scheduled ticks and did not execute for roughly eleven hours, so there
was no live coverage that day. A cloud scheduled function would not have that failure mode,
which is why the next iteration moves in that direction.

---

## What I verified before trusting output

- Profit and loss compared line by line against the EasyEquities app before the numbers
  were relied on. A plausible formula and a correct one look identical until checked
  against real figures.
- Every share count re-confirmed by hand against the live account. All were correct, which
  is worth recording as much as an error would be.
- Both failure paths tested deliberately, not just the happy path. The stale data warning
  was verified by forcing a sync failure, and the close snapshot fix by watching it fire.
- Market hours derived from the New York timezone and checked against a December date, to
  confirm US daylight saving corrects itself rather than silently drifting twice a year.

---

## Still in development

An intraday monitor that watches only what I hold and stays silent unless something
actually happens. It runs on a twenty minute interval during market hours and reports only
when a position moves past a threshold I set, or when genuinely new news lands on one of my
holdings. Silence is the default, because a monitor that talks constantly is one you stop
reading.

A working prototype runs today. What it still needs is somewhere to run that is not an open
session on my laptop, and deeper per holding insight rather than price movement alone.
