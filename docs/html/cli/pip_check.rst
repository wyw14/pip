.. _`pip check`:

=========
pip check
=========


Usage
=====

.. tab:: Unix/macOS

   .. pip-command-usage:: check "python -m pip"

.. tab:: Windows

   .. pip-command-usage:: check "py -m pip"


Description
===========

.. pip-command-description:: check


Examples
========

#. If all dependencies are compatible:

   .. tab:: Unix/macOS

      .. code-block:: console

         $ python -m pip check
         No broken requirements found.
         $ echo $?
         0

   .. tab:: Windows

      .. code-block:: console

         C:\> py -m pip check
         No broken requirements found.
         C:\> echo %errorlevel%
         0

#. If a package is missing:

   .. tab:: Unix/macOS

      .. code-block:: console

         $ python -m pip check
         pyramid 1.5.2 requires WebOb, which is not installed.
         $ echo $?
         1

   .. tab:: Windows

      .. code-block:: console

         C:\> py -m pip check
         pyramid 1.5.2 requires WebOb, which is not installed.
         C:\> echo %errorlevel%
         1

#. If a package has the wrong version:

   .. tab:: Unix/macOS

      .. code-block:: console

         $ python -m pip check
         pyramid 1.5.2 has requirement WebOb>=1.3.1, but you have WebOb 0.8.
         $ echo $?
         1

   .. tab:: Windows

      .. code-block:: console

         C:\> py -m pip check
         pyramid 1.5.2 has requirement WebOb>=1.3.1, but you have WebOb 0.8.
         C:\> echo %errorlevel%
         1

#. To see the effective dependency paths from an installed top-level project
   down to a conflicting package, pass its name to ``--explain``. Extras can
   be selected explicitly (``project[extra]``); each printed edge keeps its
   version specifier, environment marker and requested extras. The path
   search is bounded, so dependency cycles cannot make it run forever:

   .. tab:: Unix/macOS

      .. code-block:: console

         $ python -m pip check --explain "pyramid[secure]"
         WebOb 0.8 does not satisfy WebOb>=1.3.1, required on these paths from pyramid[secure] 1.5.2:
           pyramid 1.5.2
             -> intermediary 2.0  [intermediary>=1.0; python_version >= "3"]
             -> webob 0.8  [WebOb>=1.3.1]
         $ echo $?
         1

   .. tab:: Windows

      .. code-block:: console

         C:\> py -m pip check --explain "pyramid[secure]"
         WebOb 0.8 does not satisfy WebOb>=1.3.1, required on these paths from pyramid[secure] 1.5.2:
           pyramid 1.5.2
             -> intermediary 2.0  [intermediary>=1.0; python_version >= "3"]
             -> webob 0.8  [WebOb>=1.3.1]
         C:\> echo %errorlevel%
         1

