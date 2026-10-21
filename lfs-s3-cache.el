;;; lfs-s3-cache.el --- On-demand Citar and Org LFS files -*- lexical-binding: t; -*-
;; SPDX-License-Identifier: GPL-3.0-or-later
;; Version: 0.1.0
;; Package-Requires: ((emacs "28.1") (citar "1.4"))
;; Keywords: files, bib

;;; Commentary:
;; Discover tracked cold documents and hydrate them before Citar or Org opens.

;;; Code:

(require 'cl-lib)
(require 'json)
(require 'subr-x)
(declare-function citar-file--make-filename-regexp "citar-file" (keys extensions &optional sep))
(defvar lfs-s3-cache-mode)

(defgroup lfs-s3-cache nil "On-demand local Git LFS documents." :group 'files)
(defcustom lfs-s3-cache-repositories nil
  "Repositories explicitly activated with the lfs-s3-cache CLI."
  :type '(repeat directory))
(defcustom lfs-s3-cache-program "lfs-s3-cache"
  "Cache CLI executable." :type 'file)
(defvar lfs-s3-cache--catalogs (make-hash-table :test #'equal))

(defun lfs-s3-cache--call (repo &rest args)
  "Run ARGS in REPO, returning output or raising a readable error."
  (with-temp-buffer
    (let ((status (apply #'process-file lfs-s3-cache-program nil t nil
                         "--repo" (expand-file-name repo) args)))
      (unless (eq status 0)
        (user-error "LFS cache: %s" (string-trim (buffer-string))))
      (buffer-string))))

(defun lfs-s3-cache--files (repo)
  "Return tracked managed paths, including absent paths, in REPO."
  (let* ((repo (file-name-as-directory (expand-file-name repo)))
         (attrs (file-attributes (expand-file-name ".git/index" repo)))
         (stamp (list (file-attribute-modification-time attrs)
                      (file-attribute-size attrs)))
         (cached (gethash repo lfs-s3-cache--catalogs)))
    (if (equal stamp (car cached)) (cdr cached)
      (let* ((json-object-type 'alist)
             (json-array-type 'list)
             (data (json-read-from-string (lfs-s3-cache--call repo "status" "--json")))
             (files (mapcar (lambda (entry) (expand-file-name (alist-get 'path entry) repo))
                            (alist-get 'files data))))
        (puthash repo (cons stamp files) lfs-s3-cache--catalogs)
        files))))

(defun lfs-s3-cache--repository (file)
  "Return configured repository managing FILE, or nil."
  (unless (file-remote-p file)
    (let ((file (expand-file-name file)))
      (cl-find-if
       (lambda (repo)
         (and (string-prefix-p (file-name-as-directory (expand-file-name repo)) file)
              (member file (lfs-s3-cache--files repo))))
       lfs-s3-cache-repositories))))

(defun lfs-s3-cache--hydrate (file)
  "Hydrate FILE if managed and record the open, including warm opens."
  (when-let* ((repo (lfs-s3-cache--repository file)))
    (message "Opening cached document %s..." (file-name-nondirectory file))
    (lfs-s3-cache--call repo "hydrate" (expand-file-name file))))

(defun lfs-s3-cache--citar-directory (original dirs &optional keys extensions sep)
  "Add tracked absent files to ORIGINAL's Citar directory result."
  (let ((result (funcall original dirs keys extensions sep))
        (regexp (citar-file--make-filename-regexp keys extensions sep))
        (dirs (mapcar (lambda (dir) (file-name-as-directory (expand-file-name dir))) dirs)))
    (dolist (repo lfs-s3-cache-repositories)
      (when (cl-some (lambda (dir)
                       (cl-some (lambda (part)
                                  (string-prefix-p
                                   (file-name-as-directory (expand-file-name part repo)) dir))
                                '("files/library" "files/images")))
                     dirs)
       (dolist (file (lfs-s3-cache--files repo))
        (let ((name (file-name-nondirectory file)))
          (when (and (member (file-name-directory file) dirs)
                     (string-match regexp name))
            (let ((key (match-string 1 name))
                  (base (file-name-base name)))
              (cl-pushnew file (gethash key result) :test #'equal)
              (unless (or keys (equal key base))
                (cl-pushnew file (gethash base result) :test #'equal))))))))
    result))

(defun lfs-s3-cache--citar-field (original files dirs)
  "Include managed absent FILES from a bibliography file field."
  (let ((result (funcall original files dirs)))
    (dolist (file files)
      (let ((candidates (if (file-name-absolute-p file) (list file)
                          (mapcar (lambda (dir) (expand-file-name file dir)) dirs))))
        (when-let* ((found (cl-find-if #'lfs-s3-cache--repository candidates)))
          (cl-pushnew found result :test #'equal))))
    result))

(defun lfs-s3-cache--citar-normalize (original paths)
  "Allow a library directory that disappeared when all its files became cold."
  (mapcan
   (lambda (path)
     (let ((dir (file-name-as-directory (expand-file-name path))))
       (if (and (not (file-exists-p dir))
                (cl-some
                 (lambda (repo)
                   (and (cl-some (lambda (part)
                                   (string-prefix-p
                                    (file-name-as-directory (expand-file-name part repo)) dir))
                                 '("files/library" "files/images"))
                        (cl-some (lambda (file) (string-prefix-p dir file))
                                 (lfs-s3-cache--files repo))))
                 lfs-s3-cache-repositories))
           (list (file-truename path))
         (funcall original (list path)))))
   (if (stringp paths) (list paths) paths)))

(defun lfs-s3-cache--org-open (file &rest _args)
  "Hydrate the local FILE before Org checks existence or launches an opener."
  ;; org-open-file receives an unescaped path; its optional :: search suffix is
  ;; parsed by Org after this advice, so strip it here for managed lookup only.
  (lfs-s3-cache--hydrate (car (split-string file "::"))))

(defun lfs-s3-cache--install-citar ()
  (when lfs-s3-cache-mode
    (advice-add 'citar-file--directory-files :around #'lfs-s3-cache--citar-directory)
    (advice-add 'citar-file--find-files-in-dirs :around #'lfs-s3-cache--citar-field)
    (advice-add 'citar-file--normalize-paths :around #'lfs-s3-cache--citar-normalize)
    (advice-add 'citar-file-open :before #'lfs-s3-cache--hydrate)))

(defun lfs-s3-cache--install-org ()
  (when lfs-s3-cache-mode
    (advice-add 'org-open-file :before #'lfs-s3-cache--org-open)))

;;;###autoload
(define-minor-mode lfs-s3-cache-mode
  "Discover cold Citar files and hydrate Citar/Org file opens."
  :global t :group 'lfs-s3-cache
  (clrhash lfs-s3-cache--catalogs)
  (if lfs-s3-cache-mode
      (progn
        (when (featurep 'citar-file) (lfs-s3-cache--install-citar))
        (when (featurep 'org) (lfs-s3-cache--install-org)))
    (advice-remove 'citar-file--directory-files #'lfs-s3-cache--citar-directory)
    (advice-remove 'citar-file--find-files-in-dirs #'lfs-s3-cache--citar-field)
    (advice-remove 'citar-file--normalize-paths #'lfs-s3-cache--citar-normalize)
    (advice-remove 'citar-file-open #'lfs-s3-cache--hydrate)
    (advice-remove 'org-open-file #'lfs-s3-cache--org-open)))

(with-eval-after-load 'citar-file (lfs-s3-cache--install-citar))
(with-eval-after-load 'org (lfs-s3-cache--install-org))

(provide 'lfs-s3-cache)
;;; lfs-s3-cache.el ends here
