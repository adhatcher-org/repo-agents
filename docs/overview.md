# Overview

The goal of this repo is to create a set of agents that can be used to monitor several repos in github outlined in the `config/repo-info.yml` file for any PRs created  by workflow processes/jobs, such as security findings, dependbot bumps, failed ci jobs... review the findings, fix, test, verify, submit pr and commit so I don't have to.  There should be a set of agents to handle specific jobs. A team lead that is in charge of onitoring github for changes/failures/PRs, and based on what needs to be done, assigns tasks to one or more agents to complete the task. 

The goal is for this to be an automated process that runs inside a docker container.  The docker container has a /projects folder where it can clone the repods, ame changes, run tests and then commit the code from.

Issues involving major architectural changes to be brought to my attention for me to make a decision. Minor chnages should be documented and committed, noting any changes in application architecture/behavior based on the changes implemented (moved secrets into a vault vs keeping them in a local secret file...)

TL;DR: I don't want to have to deal with testing an dmerging pr's that are bumping versions of python libraries, dealing with failed CI jobs b/c the version of x/y/z thing is no longer valid or deal with simple security related issues where vulnerabilities have been discovered that can easliy be addressed.  This hould all be done automatically by a set of agents that are able to handle tasks, check each others work and resolve these issues.
