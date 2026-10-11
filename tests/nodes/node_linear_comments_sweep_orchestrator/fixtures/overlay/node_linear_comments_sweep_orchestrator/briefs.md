<!-- prompt:linear-comments-collect -->
COLLECT since @@since@@ rules @@rules@@ report @@report_path@@
<!-- end:linear-comments-collect -->
<!-- prompt:linear-comments-verify -->
VERIFY @@ticket@@ @@comment_id@@ by @@author_role@@ rules @@rules@@ facts @@facts@@ text @@comment_text@@ draft @@draft_json@@
<!-- end:linear-comments-verify -->
<!-- prompt:linear-comments-write -->
WRITE since @@since@@ to @@report_path@@ and @@metrics_path@@ rules @@rules@@ payload @@payload_json@@ metrics @@metrics_json@@
<!-- end:linear-comments-write -->
