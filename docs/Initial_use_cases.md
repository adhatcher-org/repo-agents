# Initial use cases

1. PR created by dependbot in ready to review state and no merge conflicts. (Example: [https://github.com/adhatcher-org/college_planner/pulls](https://github.com/adhatcher-org/college_planner/pulls))
2. PR created by dependbot in ready to review state with merge conflicts
3. codeql findings (Example: [https://github.com/adhatcher-org/financial_analysis/security/code-scanning/10](https://github.com/adhatcher-org/financial_analysis/security/code-scanning/10))
4. Security findings. [https://github.com/adhatcher-org/frontend_api/security/dependabot/20](https://github.com/adhatcher-org/frontend_api/security/dependabot/20)

**Ultimate goal**:  I don't want to have to deal with maintenance activitites if they don't impact the application. So things like library bumps should be merged automatically, as long as the tests are successful.

Maybe we need to consider setting up a test environment where PR's can be pulled, tested against the test environment and then approved?
