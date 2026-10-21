;; SPDX-License-Identifier: GPL-3.0-or-later
;; Build this checkout with: guix build -f guix.scm
(use-modules (guix utils))
(add-to-load-path (string-append (current-source-directory) "/guix"))
(use-modules (lfs-s3-cache packages))
lfs-s3-cache
