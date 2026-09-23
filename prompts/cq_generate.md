You are helping to specify an ontology of the EU Artificial Intelligence Act (Regulation (EU)
2024/1689). Draft competency questions: the questions the finished ontology must be able to answer.

A competency question is a question a domain expert would actually ask, phrased in their words, that
the ontology can answer from its own content. It is not a question about the ontology's structure.

## What makes a usable competency question

- **Answerable from the modelled scope.** The ontology covers Articles 3, 5, 6, 8 to 17, 26, 27, 50
  to 55, 64 and 70, plus Annexes I and III. A question about conformity assessment bodies or
  penalties cannot be answered and is out of scope.
- **Answerable as a query, not a judgement.** "Which obligations does a deployer hold?" works.
  "Is this system safe enough?" does not.
- **Returning a non-empty answer.** A question whose answer is always empty tells you nothing about
  the ontology.
- **Discriminating.** A good question fails against a wrong model. "Which obligations fall on a
  provider, not a deployer?" tests that bearers are modelled; "list all entities" does not.
- **Covering different shapes of knowledge.** Aim for a mix: enumeration (what are the prohibited
  practices?), attribution (who holds this duty?), classification (is this system high-risk, and
  why?), and traceability (which article and which words support this?).

## Output

Return 5 to 8 questions. For each, give:

- `question`: one sentence in the words a compliance officer or legal engineer would use.
- `answer_shape`: what a correct answer looks like, e.g. "a list of obligations with their article
  references".
- `entities`: the ontology classes and properties the answer would draw on.
- `needs_reasoning`: true when the answer depends on inference (for example a system's risk class
  being derived from the area it is used in) and not on asserted facts alone.

At least one question must have `needs_reasoning: true`, because a question that never needs the
reasoner would not justify an OWL ontology.
