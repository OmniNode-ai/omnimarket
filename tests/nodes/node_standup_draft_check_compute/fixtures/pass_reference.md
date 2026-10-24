Users can now bring their own model key and see what each delegated task cost; yesterday that view was empty.

*What users can do now:* The dashboard shows cost and savings for every delegated task, including tasks that ran on the lab's free model. (about 9 changes)
*Reliability:* Delegation no longer stalls when the message service restarts, because services reconnect on their own. (about 6 changes)
*Speed of shipping:* Approved changes reach users about an hour sooner because the automatic checks now run side by side. (about 15 changes)

M4 delegation: 8 of 9 checks pass; the Claude Code plugin check has failed since Oct 16, and 12 of 20 build tasks are done.

Still blocked: the Claude Code plugin cannot delegate yet; the cloud owner must approve <https://example.test/pull/1904|the dashboard token fix>, which the automatic system cannot approve itself.
