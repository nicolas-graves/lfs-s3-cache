;; SPDX-License-Identifier: GPL-3.0-or-later

(define-module (lfs-s3-cache packages)
  #:use-module (guix packages)
  #:use-module (guix gexp)
  #:use-module (guix utils)
  #:use-module ((guix licenses) #:prefix license:)
  #:use-module (guix build-system pyproject)
  #:use-module (guix build-system emacs)
  #:use-module (gnu packages emacs)
  #:use-module (gnu packages emacs-xyz)
  #:use-module (gnu packages python-build)
  #:use-module (gnu packages version-control)
  #:use-module (gnu packages shellutils)
  #:export (lfs-s3-cache emacs-lfs-s3-cache))

;; Replace this local checkout with a pinned origin when publishing a source repo.
;; The selection includes uncommitted sources, but no Git/build metadata or secrets.
(define %source
  (let* ((module-file (search-path %load-path "lfs-s3-cache/packages.scm"))
         (root (canonicalize-path (dirname (dirname (dirname module-file))))))
    (local-file
     root "lfs-s3-cache-source" #:recursive? #t
     #:select?
     (lambda (file stat)
       (let ((relative (if (string=? file root) ""
                           (substring file (+ 1 (string-length root))))))
         (member relative
                 '("" "COPYING" "README.md" "PLAN.md" "pyproject.toml"
                   "lfs_s3_cache.py" "lfs_s3_cache_daily.py" "lfs-s3-cache"
                   "lfs-s3-cache.el" "fake_lfs_agent.py"
                   "test_lfs_s3_cache.py" "test-lfs-s3-cache.el"
                   "guix.scm" "guix" "guix/lfs-s3-cache"
                   "guix/lfs-s3-cache/packages.scm")))))))

(define-public lfs-s3-cache
  (package
    (name "lfs-s3-cache")
    (version "0.1.0")
    (source %source)
    (build-system pyproject-build-system)
    (arguments
     (list
      #:phases
      #~(modify-phases %standard-phases
          (replace 'check
            (lambda* (#:key tests? inputs outputs #:allow-other-keys)
              (when tests?
                (setenv "HOME" (string-append (getcwd) "/test-home"))
                (mkdir-p (getenv "HOME"))
                (setenv "CITAR_LISP_DIR"
                        (dirname
                         (car (find-files (assoc-ref inputs "emacs-citar")
                                          "^citar-file\\.el$"))))
                (setenv "LFS_CACHE_REQUIRE_EMACS_TESTS" "1")
                (setenv "LFS_CACHE_TEST_PROGRAM"
                        (string-append (assoc-ref outputs "out")
                                       "/bin/lfs-s3-cache"))
                (invoke "python" "-m" "unittest" "-v"))))
          (add-after 'wrap 'wrap-runtime-tools
            (lambda* (#:key inputs outputs #:allow-other-keys)
              (let ((out (assoc-ref outputs "out")))
                (for-each
                 (lambda (command)
                   (wrap-program (string-append out "/bin/" command)
                     `("PATH" prefix
                       ,(map (lambda (name)
                               (string-append (assoc-ref inputs name) "/bin"))
                             '("git-minimal" "git-lfs" "direnv")))))
                 '("lfs-s3-cache" "lfs-s3-cache-daily")))))
          (add-after 'install 'install-documentation
            (lambda* (#:key outputs #:allow-other-keys)
              ;; The wheel includes raw Elisp for pip users.  Guix uses the
              ;; separate Emacs package, avoiding two competing load paths.
              (delete-file-recursively
               (string-append (assoc-ref outputs "out") "/share/emacs"))
              (let ((doc (string-append (assoc-ref outputs "out")
                                        "/share/doc/lfs-s3-cache")))
                (install-file "README.md" doc)
                (install-file "COPYING" doc)))))))
    (native-inputs (list python-setuptools emacs-minimal emacs-citar))
    (inputs (list git-minimal git-lfs direnv))
    (home-page "https://github.com/nicolas-graves/lfs-s3-cache")
    (synopsis "Manage an optional local cache of Git LFS documents")
    (description
     "This package provides conservative on-demand hydration and eviction of
Git LFS documents using an existing transfer agent.  It tracks managed opens and
pins, verifies remote recovery before eviction, and preserves untracked files
and staged or unstaged changes.  It also provides a bounded daily GC command.")
    (license license:gpl3+)))

(define-public emacs-lfs-s3-cache
  (package
    (name "emacs-lfs-s3-cache")
    (version (package-version lfs-s3-cache))
    (source (package-source lfs-s3-cache))
    (build-system emacs-build-system)
    (arguments
     (list
      #:include #~'("^lfs-s3-cache(-autoloads|-pkg)?\\.el$")
      #:phases
      #~(modify-phases %standard-phases
          (delete 'check) ; Full integration checks run in the CLI dependency.
          (add-after 'unpack 'use-packaged-cli
            (lambda* (#:key inputs #:allow-other-keys)
              (substitute* "lfs-s3-cache.el"
                (("\"lfs-s3-cache\"")
                 (string-append "\"" (assoc-ref inputs "lfs-s3-cache")
                                "/bin/lfs-s3-cache\"")))))
          (add-after 'validate-compiled-autoloads 'check
            (lambda* (#:key tests? inputs outputs #:allow-other-keys)
              (when tests?
                (for-each
                 (lambda (pattern)
                   (when (null? (find-files (assoc-ref outputs "out") pattern))
                     (error "Missing Emacs package artifact" pattern)))
                 '("^lfs-s3-cache-autoloads\\.elc$" "^lfs-s3-cache-pkg\\.el$"))
                (setenv "LFS_CACHE_EXPECTED_PROGRAM"
                        (string-append (assoc-ref inputs "lfs-s3-cache")
                                       "/bin/lfs-s3-cache"))
                (invoke "emacs" "-Q" "--batch" "-L"
                        (dirname (car (find-files (assoc-ref outputs "out")
                                                  "^lfs-s3-cache\\.elc$")))
                        "--eval"
                        "(progn (require 'lfs-s3-cache) (unless (and (equal lfs-s3-cache-program (getenv \"LFS_CACHE_EXPECTED_PROGRAM\")) (file-executable-p lfs-s3-cache-program)) (error \"Incorrect packaged CLI\")))")))))))
    (inputs (list lfs-s3-cache))
    (propagated-inputs (list emacs-citar))
    (home-page (package-home-page lfs-s3-cache))
    (synopsis "Open cached Git LFS documents from Citar and Org")
    (description
     "This Emacs integration lists tracked citation documents even when absent
locally and hydrates them before Citar or Org opens them.  It records warm opens
as well and uses the matching packaged lfs-s3-cache command.")
    (license license:gpl3+)))
